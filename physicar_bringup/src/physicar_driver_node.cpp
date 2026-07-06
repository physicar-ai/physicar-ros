// Copyright 2026 AICASTLE Inc.
// SPDX-License-Identifier: GPL-3.0-or-later
//
// PhysiCar Base Driver ROS2 Node (C++)
// Integrates: Rosmaster serial protocol, GPIO PWM, servo controller, driver node

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

#include <fcntl.h>
#include <termios.h>
#include <unistd.h>
#include <sys/stat.h>
#include <sys/ioctl.h>

#include "rclcpp/rclcpp.hpp"
#include "sensor_msgs/msg/imu.hpp"
#include "sensor_msgs/msg/magnetic_field.hpp"
#include "sensor_msgs/msg/battery_state.hpp"
#include "sensor_msgs/msg/joint_state.hpp"
#include "geometry_msgs/msg/twist.hpp"
#include "nav_msgs/msg/odometry.hpp"
#include "std_msgs/msg/float64.hpp"
#include "std_msgs/msg/float64_multi_array.hpp"
#include "std_srvs/srv/trigger.hpp"

// Custom interfaces (optional — HAS_CUSTOM_INTERFACES defined by CMake)
#ifdef HAS_CUSTOM_INTERFACES
  #include "physicar_interfaces/srv/set_calibration.hpp"
  #include "physicar_interfaces/srv/get_calibration.hpp"
  #include "physicar_interfaces/msg/calibration_status.hpp"
  #include "physicar_interfaces/msg/teleop_status.hpp"
  #define HAS_TELEOP_STATUS 1
#else
  #define HAS_TELEOP_STATUS 0
#endif

using namespace std::chrono_literals;
using std::placeholders::_1;
using std::placeholders::_2;

// ═══════════════════════════════════════════════════════════
// Rosmaster Board — serial protocol (replaces Rosmaster_Lib)
// ═══════════════════════════════════════════════════════════

class RosmasterBoard {
public:
  struct ImuData {
    double ax = 0, ay = 0, az = 0;   // m/s² (accel)
    double gx = 0, gy = 0, gz = 0;   // rad/s (gyro)
    double mx = 0, my = 0, mz = 0;   // µT   (mag)
    double roll = 0, pitch = 0, yaw = 0; // rad (attitude)
  };

  explicit RosmasterBoard(const std::string & port = "/dev/yahboom")
  : port_(port) {}

  ~RosmasterBoard() { disconnect(); }

  bool connect() {
    // Open with O_NONBLOCK then clear it — matches pyserial exactly
    fd_ = ::open(port_.c_str(), O_RDWR | O_NOCTTY | O_NONBLOCK);
    if (fd_ < 0) return false;
    fcntl(fd_, F_SETFL, 0);  // clear O_NONBLOCK for blocking reads

    struct termios tty;
    if (tcgetattr(fd_, &tty) != 0) {
      ::close(fd_); fd_ = -1;
      return false;
    }

    // Input flags — match pyserial: clear all processing & flow control
    tty.c_iflag &= ~(INLCR | IGNCR | ICRNL | IGNBRK | PARMRK |
                      INPCK | ISTRIP | IXON | IXOFF | IXANY);

    // Output flags — raw output
    tty.c_oflag &= ~(OPOST | ONLCR | OCRNL);

    // Control flags — 8N1, no flow control, enable receiver
    tty.c_cflag &= ~(CSIZE | PARENB | PARODD | CSTOPB | CRTSCTS);
    tty.c_cflag |= (CS8 | CLOCAL | CREAD);

    // Local flags — raw mode, no echo
    tty.c_lflag &= ~(ICANON | ECHO | ECHOE | ECHOK | ECHONL |
                      ISIG | IEXTEN);

    // Blocking read, 1 byte minimum
    tty.c_cc[VMIN] = 1;
    tty.c_cc[VTIME] = 0;

    // Baud rate
    cfsetispeed(&tty, B115200);
    cfsetospeed(&tty, B115200);

    if (tcsetattr(fd_, TCSANOW, &tty) != 0) {
      ::close(fd_); fd_ = -1;
      return false;
    }

    // Assert DTR and RTS — pyserial does this by default
    int modem_bits = TIOCM_DTR | TIOCM_RTS;
    ioctl(fd_, TIOCMBIS, &modem_bits);

    // Flush input/output buffers
    tcflush(fd_, TCIOFLUSH);

    connected_ = true;

    // Start receive thread
    rx_thread_ = std::thread(&RosmasterBoard::receive_loop, this);

    std::this_thread::sleep_for(500ms);
    return true;
  }

  void disconnect() {
    connected_ = false;
    if (fd_ >= 0) { ::close(fd_); fd_ = -1; }  // close first to unblock read()
    if (rx_thread_.joinable()) rx_thread_.join();
  }

  bool is_connected() const { return connected_ && fd_ >= 0; }

  // ── TX commands ──

  bool set_pwm_servo(int channel, int angle) {
    if (!is_connected() || channel < 1 || channel > 4) return false;
    angle = std::clamp(angle, 0, 180);
    uint8_t cmd[] = {HEAD, DEVICE_ID, 0x00, FUNC_PWM_SERVO,
                     static_cast<uint8_t>(channel),
                     static_cast<uint8_t>(angle)};
    cmd[2] = sizeof(cmd) - 1;  // length
    return send_cmd(cmd, sizeof(cmd));
  }

  bool set_beep(int duration_ms) {
    if (!is_connected()) return false;
    int16_t d = static_cast<int16_t>(duration_ms);
    uint8_t cmd[] = {HEAD, DEVICE_ID, 0x05, FUNC_BEEP,
                     static_cast<uint8_t>(d & 0xFF),
                     static_cast<uint8_t>((d >> 8) & 0xFF)};
    return send_cmd(cmd, sizeof(cmd));
  }

  // ── RX data (thread-safe reads) ──

  ImuData read_imu() const {
    std::lock_guard<std::mutex> lk(data_mutex_);
    return imu_;
  }

  double read_battery_voltage() const {
    std::lock_guard<std::mutex> lk(data_mutex_);
    return battery_voltage_;
  }

private:
  static constexpr uint8_t HEAD        = 0xFF;
  static constexpr uint8_t DEVICE_ID   = 0xFC;
  static constexpr uint8_t COMPLEMENT  = 257 - DEVICE_ID;  // 0x05

  // Function codes
  static constexpr uint8_t FUNC_BEEP            = 0x02;
  static constexpr uint8_t FUNC_PWM_SERVO       = 0x03;
  static constexpr uint8_t FUNC_REPORT_SPEED    = 0x0A;
  static constexpr uint8_t FUNC_REPORT_MPU_RAW  = 0x0B;
  static constexpr uint8_t FUNC_REPORT_IMU_ATT  = 0x0C;
  static constexpr uint8_t FUNC_REPORT_ICM_RAW  = 0x0E;

  bool send_cmd(const uint8_t * cmd, size_t len) {
    uint16_t sum = COMPLEMENT;
    for (size_t i = 0; i < len; ++i) sum += cmd[i];
    uint8_t checksum = sum & 0xFF;

    std::lock_guard<std::mutex> lk(tx_mutex_);
    if (::write(fd_, cmd, len) < 0) return false;
    if (::write(fd_, &checksum, 1) < 0) return false;
    // 2ms delay (matches Rosmaster_Lib timing)
    std::this_thread::sleep_for(2ms);
    return true;
  }

  void receive_loop() {
    // Flush input buffer at thread start (matches Python flushInput())
    tcflush(fd_, TCIFLUSH);

    int total_bytes = 0;
    int valid_frames = 0;

    while (connected_) {
      uint8_t b;
      if (!read_byte(b)) {
        if (!connected_) break;  // fd closed during disconnect
        continue;
      }
      total_bytes++;

      // Diagnostic: log first 20 bytes and periodic stats
      if (total_bytes <= 20) {
        fprintf(stderr, "[serial-rx] byte #%d: 0x%02X\n", total_bytes, b);
      } else if (total_bytes == 21) {
        fprintf(stderr, "[serial-rx] receiving data OK, suppressing byte log\n");
      }

      if (b == HEAD) {
        if (!read_byte(b)) continue;
        total_bytes++;
        if (b != (DEVICE_ID - 1)) continue;  // 0xFB

        uint8_t ext_len, ext_type;
        if (!read_byte(ext_len) || !read_byte(ext_type)) continue;
        total_bytes += 2;

        int data_len = ext_len - 2;
        if (data_len < 0 || data_len > 64) continue;

        std::vector<uint8_t> data(data_len);
        bool ok = true;
        for (int i = 0; i < data_len; ++i) {
          if (!read_byte(data[i])) { ok = false; break; }
          total_bytes++;
        }
        if (!ok) continue;

        // Verify checksum: sum of (ext_len + ext_type + data[0..n-2]) mod 256 == data[n-1]
        uint16_t sum = ext_len + ext_type;
        for (int i = 0; i < data_len - 1; ++i) sum += data[i];
        if ((sum & 0xFF) != data[data_len - 1]) {
          if (valid_frames == 0) {
            fprintf(stderr, "[serial-rx] checksum fail: func=0x%02X len=%d expected=0x%02X got=0x%02X\n",
                    ext_type, ext_len, (sum & 0xFF), data[data_len - 1]);
          }
          continue;
        }

        valid_frames++;
        if (valid_frames == 1) {
          fprintf(stderr, "[serial-rx] first valid frame: func=0x%02X len=%d (after %d bytes)\n",
                  ext_type, ext_len, total_bytes);
        }

        parse_data(ext_type, data.data(), data_len - 1);
      }
    }
    fprintf(stderr, "[serial-rx] thread exit: %d bytes, %d frames\n", total_bytes, valid_frames);
  }

  bool read_byte(uint8_t & out) {
    ssize_t n = ::read(fd_, &out, 1);
    return n == 1;
  }

  static int16_t unpack_i16(const uint8_t * p) {
    int16_t v;
    std::memcpy(&v, p, 2);
    return v;
  }

  void parse_data(uint8_t func, const uint8_t * d, int len) {
    std::lock_guard<std::mutex> lk(data_mutex_);

    if (func == FUNC_REPORT_SPEED && len >= 7) {
      battery_voltage_ = d[6] / 10.0;
    }
    else if (func == FUNC_REPORT_MPU_RAW && len >= 18) {
      // MPU9250: gyro ±500dps → rad/s
      constexpr double gyro_r = 1.0 / 3754.9;
      imu_.gx =  unpack_i16(d + 0) * gyro_r;
      imu_.gy = -unpack_i16(d + 2) * gyro_r;
      imu_.gz = -unpack_i16(d + 4) * gyro_r;
      // accel ±2g
      constexpr double acc_r = 1.0 / 1671.84;
      imu_.ax = unpack_i16(d + 6)  * acc_r;
      imu_.ay = unpack_i16(d + 8)  * acc_r;
      imu_.az = unpack_i16(d + 10) * acc_r;
      // mag
      imu_.mx = unpack_i16(d + 12);
      imu_.my = unpack_i16(d + 14);
      imu_.mz = unpack_i16(d + 16);
    }
    else if (func == FUNC_REPORT_ICM_RAW && len >= 18) {
      // ICM20948: gyro/accel/mag in milli-units
      constexpr double r = 1.0 / 1000.0;
      imu_.gx = unpack_i16(d + 0) * r;
      imu_.gy = unpack_i16(d + 2) * r;
      imu_.gz = unpack_i16(d + 4) * r;
      imu_.ax = unpack_i16(d + 6)  * r;
      imu_.ay = unpack_i16(d + 8)  * r;
      imu_.az = unpack_i16(d + 10) * r;
      imu_.mx = unpack_i16(d + 12) * r;
      imu_.my = unpack_i16(d + 14) * r;
      imu_.mz = unpack_i16(d + 16) * r;
    }
    else if (func == FUNC_REPORT_IMU_ATT && len >= 6) {
      imu_.roll  = unpack_i16(d + 0) / 10000.0;
      imu_.pitch = unpack_i16(d + 2) / 10000.0;
      imu_.yaw   = unpack_i16(d + 4) / 10000.0;
    }
  }

  std::string port_;
  int fd_ = -1;
  std::atomic<bool> connected_{false};
  std::thread rx_thread_;
  mutable std::mutex data_mutex_;
  std::mutex tx_mutex_;
  ImuData imu_;
  double battery_voltage_ = 0.0;
};

// ═══════════════════════════════════════════════════════════
// GPIO PWM Board — sysfs hardware PWM
// ═══════════════════════════════════════════════════════════

class GpioPwmBoard {
public:
  static constexpr int PERIOD_NS     = 20'000'000;
  static constexpr int MIN_DUTY_NS   = 500'000;
  static constexpr int DUTY_RANGE_NS = 2'000'000;

  bool connect(rclcpp::Logger logger) {
    for (auto & [ch, idx] : kChannelMap) {
      std::string pwm_dir = pwm_path(idx);
      if (!dir_exists(pwm_dir)) {
        write_file("/sys/class/pwm/pwmchip0/export", std::to_string(idx));
      }
      write_file(pwm_path(idx, "period"), std::to_string(PERIOD_NS));
      int neutral = MIN_DUTY_NS + DUTY_RANGE_NS / 2;
      write_file(pwm_path(idx, "duty_cycle"), std::to_string(neutral));
      write_file(pwm_path(idx, "enable"), "1");
    }
    connected_ = true;
    RCLCPP_INFO(logger, "GpioPwmBoard: connected (pwm0=steering, pwm1=ESC)");
    return true;
  }

  void disconnect() {
    int neutral = MIN_DUTY_NS + DUTY_RANGE_NS / 2;
    for (auto & [ch, idx] : kChannelMap) {
      if (dir_exists(pwm_path(idx))) {
        // Hold neutral but do NOT write enable=0: cutting the pulse train lets the
        // pin settle and the ESC can catch a spurious throttle -> brief twitch on
        // shutdown/restart. Keep emitting neutral until power is actually cut.
        write_file(pwm_path(idx, "duty_cycle"), std::to_string(neutral));
      }
    }
    connected_ = false;
  }

  bool is_connected() const { return connected_; }

  bool set_servo(int channel, double angle) {
    auto it = kChannelMap.find(channel);
    if (it == kChannelMap.end()) return false;
    angle = std::clamp(angle, 0.0, 180.0);
    int duty = static_cast<int>(MIN_DUTY_NS + (angle / 180.0) * DUTY_RANGE_NS);
    return write_file(pwm_path(it->second, "duty_cycle"), std::to_string(duty));
  }

  bool set_duty_ns(int channel, int duty_ns) {
    auto it = kChannelMap.find(channel);
    if (it == kChannelMap.end()) return false;
    duty_ns = std::clamp(duty_ns, MIN_DUTY_NS, MIN_DUTY_NS + DUTY_RANGE_NS);
    return write_file(pwm_path(it->second, "duty_cycle"), std::to_string(duty_ns));
  }

private:
  // Channel 1 (Throttle/ESC) → pwm1 (GPIO13)
  // Channel 2 (Steering)     → pwm0 (GPIO12)
  static inline const std::map<int,int> kChannelMap = {{1,1}, {2,0}};
  bool connected_ = false;

  static std::string pwm_path(int idx, const std::string & attr = "") {
    std::string p = "/sys/class/pwm/pwmchip0/pwm" + std::to_string(idx);
    if (!attr.empty()) p += "/" + attr;
    return p;
  }

  static bool dir_exists(const std::string & p) {
    struct stat st{};
    return stat(p.c_str(), &st) == 0 && S_ISDIR(st.st_mode);
  }

  static bool write_file(const std::string & path, const std::string & val) {
    std::ofstream f(path);
    if (!f) return false;
    f << val;
    return f.good();
  }
};

// ═══════════════════════════════════════════════════════════
// Servo Controller — angle mapping, ESC model, sine steering
// ═══════════════════════════════════════════════════════════

struct ServoLimits {
  double min_angle = 0, max_angle = 180, center_angle = 90;
  double clamp(double a) const { return std::clamp(a, min_angle, max_angle); }
  double from_normalized(double v) const {
    if (v >= 0) return center_angle + v * (max_angle - center_angle);
    else        return center_angle + v * (center_angle - min_angle);
  }
};

class ServoController {
public:
  static constexpr int CH_THROTTLE = 1, CH_STEERING = 2, CH_PAN = 3, CH_TILT = 4;

  // ESC power-law model constants
  static constexpr double ESC_A1 = 37937, ESC_K1 = 1.150, ESC_D1 = 73177, ESC_P1 = 0.810;
  static constexpr double ESC_A2 = 37773, ESC_K2 = 1.186, ESC_D2 = 93981, ESC_P2 = 0.913;
  static constexpr double ESC_REF_V = 7.4;
  static constexpr int    ESC_CENTER_NS = 1'500'000;
  static constexpr double SERVO_CENTER = 90.0;
  static constexpr double DEFAULT_STEERING_RATIO = 0.96;

  RosmasterBoard * board = nullptr;
  GpioPwmBoard   * drive_board = nullptr;

  std::map<int, ServoLimits> limits;
  std::map<int, double> trim;
  std::map<int, bool>   inverted;

  double steering_ratio = DEFAULT_STEERING_RATIO;
  double speed_gain = 1.0;

  ServoController() {
    for (int ch : {CH_THROTTLE, CH_STEERING, CH_PAN, CH_TILT}) {
      limits[ch] = {90, 90, 90};
      trim[ch] = 0;
      inverted[ch] = false;
    }
  }

  void set_limits(int ch, double lo, double hi, double ctr = 90.0) {
    limits[ch] = {lo, hi, ctr};
  }
  void set_trim(int ch, double t) { trim[ch] = t; }
  void set_inverted(int ch, bool inv) { inverted[ch] = inv; }

  bool apply_angle(int ch, double angle) {
    if (inverted[ch]) angle = 180.0 - angle;
    angle = limits[ch].clamp(angle);
    angle += trim[ch];
    if (drive_board && (ch == CH_THROTTLE || ch == CH_STEERING))
      return drive_board->set_servo(ch, angle);
    return board ? board->set_pwm_servo(ch, static_cast<int>(std::round(angle))) : false;
  }

  bool set_steering_wheel_angle(double wheel_deg) {
    if (std::abs(wheel_deg) < 0.001)
      return apply_angle(CH_STEERING, SERVO_CENTER);
    double sin_servo = std::sin(wheel_deg * M_PI / 180.0) / steering_ratio;
    sin_servo = std::clamp(sin_servo, -1.0, 1.0);
    double offset = std::asin(sin_servo) * 180.0 / M_PI;
    return apply_angle(CH_STEERING, SERVO_CENTER + offset);
  }

  bool set_throttle_speed(double speed, double voltage = 7.4) {
    if (std::abs(speed) < 0.001) {
      if (drive_board)
        return drive_board->set_duty_ns(CH_THROTTLE, ESC_CENTER_NS);
      return apply_angle(CH_THROTTLE, SERVO_CENTER);
    }

    bool rev = inverted[CH_THROTTLE];
    double a, k, d, p;
    if (rev) { a = ESC_A2; k = ESC_K2; d = ESC_D2; p = ESC_P2; }
    else     { a = ESC_A1; k = ESC_K1; d = ESC_D1; p = ESC_P1; }

    double g_dir;
    if (rev)  g_dir = (speed > 0) ? speed_gain * p : speed_gain;
    else      g_dir = (speed > 0) ? speed_gain     : speed_gain * p;

    double v_comp = ESC_REF_V / std::max(voltage, 5.0);
    double offset = g_dir * (a * std::pow(std::abs(speed), k) + d) * v_comp;

    int sign = (speed > 0) ? -1 : 1;
    if (rev) sign = -sign;
    int duty_ns = static_cast<int>(ESC_CENTER_NS + sign * offset);

    if (drive_board)
      return drive_board->set_duty_ns(CH_THROTTLE, duty_ns);
    double angle = (duty_ns - 500000) / 2000000.0 * 180.0;
    return board->set_pwm_servo(CH_THROTTLE, static_cast<int>(std::round(angle)));
  }

  bool center_all() {
    bool ok = true;
    for (auto & [ch, lim] : limits)
      ok &= apply_angle(ch, lim.center_angle);
    return ok;
  }
};

// ═══════════════════════════════════════════════════════════
// Calibration data (JSON load/save)
// ═══════════════════════════════════════════════════════════

struct CalibrationData {
  double steering_center = 0, pan_center = 0, tilt_center = 0;
  bool reverse_direction = false;
  double speed_gain = 1.0;
  std::string source = "defaults";
  bool is_saved = false;

  std::string to_json() const {
    char buf[256];
    std::snprintf(buf, sizeof(buf),
      "{\n  \"steering_center\": %.2f,\n  \"pan_center\": %.2f,\n"
      "  \"tilt_center\": %.2f,\n  \"reverse_direction\": %s,\n"
      "  \"speed_gain\": %.2f\n}",
      steering_center, pan_center, tilt_center,
      reverse_direction ? "true" : "false", speed_gain);
    return buf;
  }

  bool save(const std::string & path) {
    // Ensure directory exists
    auto dir = path.substr(0, path.rfind('/'));
    if (!dir.empty()) {
      std::string cmd = "mkdir -p " + dir;
      (void)system(cmd.c_str());
    }
    std::ofstream f(path);
    if (!f) return false;
    f << to_json();
    is_saved = f.good();
    return is_saved;
  }

  static CalibrationData load_json(const std::string & path) {
    CalibrationData cal;
    std::ifstream f(path);
    if (!f) return cal;

    std::string content((std::istreambuf_iterator<char>(f)),
                         std::istreambuf_iterator<char>());

    // Minimal JSON parser (no external lib needed for this flat structure)
    auto get_double = [&](const std::string & key, double def) -> double {
      auto pos = content.find("\"" + key + "\"");
      if (pos == std::string::npos) return def;
      pos = content.find(':', pos);
      if (pos == std::string::npos) return def;
      try { return std::stod(content.substr(pos + 1)); }
      catch (...) { return def; }
    };
    auto get_bool = [&](const std::string & key, bool def) -> bool {
      auto pos = content.find("\"" + key + "\"");
      if (pos == std::string::npos) return def;
      pos = content.find(':', pos);
      if (pos == std::string::npos) return def;
      auto sub = content.substr(pos + 1, 10);
      if (sub.find("true") != std::string::npos) return true;
      if (sub.find("false") != std::string::npos) return false;
      return def;
    };

    cal.steering_center = std::clamp(get_double("steering_center", 0.0), -30.0, 30.0);
    cal.pan_center = std::clamp(get_double("pan_center", 0.0), -30.0, 30.0);
    cal.tilt_center = std::clamp(get_double("tilt_center", 0.0), -30.0, 30.0);
    cal.reverse_direction = get_bool("reverse_direction", false);
    // Support both "speed_gain" and legacy "esc_gain"
    double sg = get_double("speed_gain", -1);
    if (sg < 0) sg = get_double("esc_gain", 1.0);
    cal.speed_gain = std::clamp(sg, 0.1, 5.0);
    cal.source = "json_file";
    cal.is_saved = true;
    return cal;
  }
};

// ═══════════════════════════════════════════════════════════
// Battery helpers
// ═══════════════════════════════════════════════════════════

static int detect_cells(double v) { return v > 9.0 ? 3 : 2; }

static int battery_percentage(double v) {
  int c = detect_cells(v);
  double lo = (c == 3) ? 9.6 : 6.4;
  double hi = (c == 3) ? 12.6 : 8.4;
  return std::clamp(static_cast<int>((v - lo) / (hi - lo) * 100), 0, 100);
}

static double battery_low_threshold(double v) {
  return detect_cells(v) == 3 ? 9.9 : 6.6;
}

// ═══════════════════════════════════════════════════════════
// PhysiCar Driver Node
// ═══════════════════════════════════════════════════════════

class PhysicarDriverNode : public rclcpp::Node {
public:
  static constexpr double MAX_STEERING = 20.0;
  static constexpr double MAX_SPEED    = 3.0;
  static constexpr double MAX_PAN      = 30.0;
  static constexpr double MAX_TILT     = 30.0;

  static constexpr double FEEDBACK_TIMEOUT     = 0.5;
  static constexpr double FEEDBACK_LOOKAHEAD   = 0.2;
  static constexpr double FEEDBACK_P_GAIN      = 1.5;
  static constexpr double FEEDBACK_MAX_ADJUST  = 0.5;
  static constexpr double SPEED_DEADZONE       = 0.01;
  static constexpr double BRAKE_SPEED          = 0.3;

  PhysicarDriverNode() : Node("physicar_driver") {
    // Parameters
    declare_parameter("serial_port", "/dev/yahboom");
    declare_parameter("baudrate", 115200);
    declare_parameter("imu_frame_id", "imu_link");
    declare_parameter("publish_rate", 50.0);
    declare_parameter("battery_publish_rate", 1.0);
    declare_parameter("battery_low_threshold", 6.6);
    declare_parameter("steering_center", 0.0);
    declare_parameter("pan_center", 0.0);
    declare_parameter("tilt_center", 0.0);
    declare_parameter("reverse_direction", false);
    declare_parameter("speed_gain", 1.0);
    declare_parameter("wheel_radius", 0.0375);
    declare_parameter("wheelbase", 0.18);
    declare_parameter("track_width", 0.16);
    declare_parameter("calibration_file",
      std::string("/opt/physicar/userdata/calibration.json"));

    auto serial_port = get_parameter("serial_port").as_string();
    imu_frame_id_ = get_parameter("imu_frame_id").as_string();
    double publish_rate = get_parameter("publish_rate").as_double();
    wheel_radius_ = get_parameter("wheel_radius").as_double();
    wheelbase_ = get_parameter("wheelbase").as_double();
    track_width_ = get_parameter("track_width").as_double();
    calibration_file_ = get_parameter("calibration_file").as_string();

    // Hardware init
    board_.reset(new RosmasterBoard(serial_port));
    servo_.board = board_.get();
    servo_.drive_board = &gpio_board_;

    // Load calibration
    load_and_apply_calibration();

    // Connect boards
    if (!board_->connect())
      RCLCPP_ERROR(get_logger(), "Failed to connect expansion board on %s", serial_port.c_str());
    else
      RCLCPP_INFO(get_logger(), "Connected to expansion board on %s", serial_port.c_str());

    if (!gpio_board_.connect(get_logger()))
      RCLCPP_ERROR(get_logger(), "Failed to connect GPIO PWM board");
    else
      initialize_esc();

    // QoS
    auto qos = rclcpp::QoS(10);
    auto qos_sensor = rclcpp::SensorDataQoS();

    // Publishers
    imu_pub_ = create_publisher<sensor_msgs::msg::Imu>("/imu", qos_sensor);
    mag_pub_ = create_publisher<sensor_msgs::msg::MagneticField>("/imu/mag", qos_sensor);
    battery_pub_ = create_publisher<sensor_msgs::msg::BatteryState>("/battery_state", qos);
    joint_pub_ = create_publisher<sensor_msgs::msg::JointState>("/joint_states", qos);

    // Subscribers (must store SharedPtr — rclcpp callback groups hold WeakPtr)
    subs_.push_back(create_subscription<std_msgs::msg::Float64>("/speed", qos,
      [this](const std_msgs::msg::Float64::SharedPtr m) {
        if (!drive_engaged()) set_speed(m->data);
      }));
    subs_.push_back(create_subscription<std_msgs::msg::Float64>("/steering", qos,
      [this](const std_msgs::msg::Float64::SharedPtr m) {
        if (!drive_engaged()) set_steering_rad(m->data);
      }));
    subs_.push_back(create_subscription<std_msgs::msg::Float64>("/teleop/speed", qos,
      [this](const std_msgs::msg::Float64::SharedPtr m) { set_speed(m->data); }));
    subs_.push_back(create_subscription<std_msgs::msg::Float64>("/teleop/steering", qos,
      [this](const std_msgs::msg::Float64::SharedPtr m) { set_steering_rad(m->data); }));
    subs_.push_back(create_subscription<geometry_msgs::msg::Twist>("/cmd_vel", qos,
      std::bind(&PhysicarDriverNode::cmd_vel_callback, this, _1)));
    subs_.push_back(create_subscription<std_msgs::msg::Float64>("/camera/pan", qos,
      [this](const std_msgs::msg::Float64::SharedPtr m) {
        if (!camera_engaged()) apply_pan(m->data);
      }));
    subs_.push_back(create_subscription<std_msgs::msg::Float64>("/camera/tilt", qos,
      [this](const std_msgs::msg::Float64::SharedPtr m) {
        if (!camera_engaged()) apply_tilt(m->data);
      }));
    subs_.push_back(create_subscription<std_msgs::msg::Float64>("/teleop/camera/pan", qos,
      [this](const std_msgs::msg::Float64::SharedPtr m) { apply_pan(m->data); }));
    subs_.push_back(create_subscription<std_msgs::msg::Float64>("/teleop/camera/tilt", qos,
      [this](const std_msgs::msg::Float64::SharedPtr m) { apply_tilt(m->data); }));
    subs_.push_back(create_subscription<std_msgs::msg::Float64MultiArray>("/servo/commands", qos,
      [this](const std_msgs::msg::Float64MultiArray::SharedPtr m) {
        if (m->data.size() >= 2) {
          int ch = static_cast<int>(m->data[0]);
          if (ch >= 1 && ch <= 4) board_->set_pwm_servo(ch, static_cast<int>(std::round(m->data[1])));
        }
      }));
    subs_.push_back(create_subscription<nav_msgs::msg::Odometry>("/odom", qos,
      std::bind(&PhysicarDriverNode::odom_callback, this, _1)));

#if HAS_TELEOP_STATUS
    subs_.push_back(create_subscription<physicar_interfaces::msg::TeleopStatus>("/teleop/status", qos,
      std::bind(&PhysicarDriverNode::teleop_status_callback, this, _1)));
#endif

    // Services
#ifdef HAS_CUSTOM_INTERFACES
    set_cal_srv_ = create_service<physicar_interfaces::srv::SetCalibration>(
      "~/set_calibration",
      std::bind(&PhysicarDriverNode::set_calibration_cb, this, _1, _2));
    get_cal_srv_ = create_service<physicar_interfaces::srv::GetCalibration>(
      "~/get_calibration",
      std::bind(&PhysicarDriverNode::get_calibration_cb, this, _1, _2));
    cal_status_pub_ = create_publisher<physicar_interfaces::msg::CalibrationStatus>(
      "~/calibration_status", qos);
    publish_calibration_status();
    RCLCPP_INFO(get_logger(), "Calibration services ready");
#else
    create_service<std_srvs::srv::Trigger>(
      "~/reload_calibration",
      [this](const std_srvs::srv::Trigger::Request::SharedPtr,
             std_srvs::srv::Trigger::Response::SharedPtr resp) {
        load_and_apply_calibration();
        resp->success = true;
        resp->message = "Calibration reloaded from " + cal_.source;
      });
#endif

    // Timers (must store SharedPtr — rclcpp callback groups hold WeakPtr)
    imu_timer_ = create_timer(1.0 / publish_rate, [this]() { publish_imu(); });
    joint_timer_ = create_timer(1.0 / publish_rate, [this]() { publish_joint_states(); });
    double batt_rate = get_parameter("battery_publish_rate").as_double();
    battery_low_thresh_ = get_parameter("battery_low_threshold").as_double();
    battery_timer_ = create_timer(1.0 / batt_rate, [this]() { publish_battery(); });

    RCLCPP_INFO(get_logger(), "PhysiCar Driver Node started");
  }

  ~PhysicarDriverNode() override {
    RCLCPP_INFO(get_logger(), "Shutting down PhysiCar Driver...");
    if (gpio_board_.is_connected()) {
      servo_.set_throttle_speed(0.0);
      servo_.center_all();
      gpio_board_.disconnect();
    }
    board_->disconnect();
  }

private:
  // ── Timer helper ──
  rclcpp::TimerBase::SharedPtr create_timer(double period_s,
    std::function<void()> cb) {
    return create_wall_timer(
      std::chrono::duration<double>(period_s), std::move(cb));
  }

  // ── Teleop aggregation ──

  struct TeleopSource {
    bool drive = false, camera = false, estop = false;
    double timeout_sec = 0.5;
    int64_t last_ns = 0;
  };
  std::map<std::string, TeleopSource> teleop_sources_;

  bool drive_engaged() {
    auto now_ns = now().nanoseconds();
    for (auto & [_, s] : teleop_sources_) {
      if ((now_ns - s.last_ns) <= static_cast<int64_t>(s.timeout_sec * 1e9) && s.drive)
        return true;
    }
    return false;
  }

  bool camera_engaged() {
    auto now_ns = now().nanoseconds();
    for (auto & [_, s] : teleop_sources_) {
      if ((now_ns - s.last_ns) <= static_cast<int64_t>(s.timeout_sec * 1e9) && s.camera)
        return true;
    }
    return false;
  }

#if HAS_TELEOP_STATUS
  void teleop_status_callback(const physicar_interfaces::msg::TeleopStatus::SharedPtr msg) {
    std::string src = msg->source.empty() ? "unknown" : msg->source;
    double timeout = msg->timeout.sec + msg->timeout.nanosec / 1e9;
    if (timeout <= 0) timeout = 0.5;
    teleop_sources_[src] = {
      msg->drive_engaged, msg->camera_engaged, msg->estop_latched,
      timeout, now().nanoseconds()
    };
  }
#endif

  // ── Speed / Steering ──

  void set_speed(double speed_mps) {
    speed_mps = std::clamp(speed_mps, -MAX_SPEED, MAX_SPEED);
    target_speed_ = speed_mps;
    current_throttle_ = speed_mps;

    if (std::abs(speed_mps) < SPEED_DEADZONE) {
      bool stale = (steady_now() - actual_speed_time_) > FEEDBACK_TIMEOUT;
      if (stale || std::abs(actual_speed_) <= BRAKE_SPEED) {
        servo_.set_throttle_speed(0.0);
        speed_adjust_ = 0;
        brake_active_ = false;
      } else {
        brake_active_ = true;
      }
    } else {
      brake_active_ = false;
      servo_.set_throttle_speed(get_adjusted_speed(), current_voltage_);
    }
  }

  void set_steering_rad(double rad) {
    double deg = rad * 180.0 / M_PI;
    deg = std::clamp(deg, -MAX_STEERING, MAX_STEERING);
    current_steering_ = deg * M_PI / 180.0;
    servo_.set_steering_wheel_angle(deg);
  }

  double get_adjusted_speed() {
    if (target_speed_ == 0) return 0;
    if (steady_now() - actual_speed_time_ > FEEDBACK_TIMEOUT) speed_adjust_ = 0;
    double adj = target_speed_ + speed_adjust_;
    return (target_speed_ > 0) ? std::max(SPEED_DEADZONE, adj)
                               : std::min(-SPEED_DEADZONE, adj);
  }

  // ── Odom feedback ──

  void odom_callback(const nav_msgs::msg::Odometry::SharedPtr msg) {
    double now_t = steady_now();
    double actual = msg->twist.twist.linear.x;
    double dt = (prev_odom_time_ > 0) ? (now_t - prev_odom_time_) : 0.0;
    double accel = (dt > 0.001) ? (actual - prev_actual_speed_) / dt : 0.0;

    prev_actual_speed_ = actual;
    prev_odom_time_ = now_t;
    actual_speed_ = actual;
    actual_speed_time_ = now_t;

    if (std::abs(target_speed_) < SPEED_DEADZONE) {
      if (!brake_active_) return;
      if (std::abs(actual) <= BRAKE_SPEED) {
        servo_.set_throttle_speed(0.0);
        speed_adjust_ = 0;
        brake_active_ = false;
      } else {
        servo_.set_throttle_speed(-std::copysign(BRAKE_SPEED, actual), current_voltage_);
      }
      return;
    }

    double predicted = actual + accel * FEEDBACK_LOOKAHEAD;
    double error = target_speed_ - predicted;
    speed_adjust_ = std::clamp(error * FEEDBACK_P_GAIN, -FEEDBACK_MAX_ADJUST, FEEDBACK_MAX_ADJUST);
    servo_.set_throttle_speed(get_adjusted_speed(), current_voltage_);
  }

  // ── cmd_vel ──

  void cmd_vel_callback(const geometry_msgs::msg::Twist::SharedPtr msg) {
    if (drive_engaged()) return;
    double vx = msg->linear.x;
    double wz = msg->angular.z;
    double steer_deg;
    if (std::abs(vx) > 0.01) {
      steer_deg = std::atan(wz * wheelbase_ / vx) * 180.0 / M_PI;
    } else if (std::abs(wz) > 0.01) {
      steer_deg = std::copysign(MAX_STEERING, wz);
    } else {
      steer_deg = 0.0;
    }
    set_speed(vx);
    set_steering_rad(steer_deg * M_PI / 180.0);
  }

  // ── Camera ──

  void apply_pan(double rad) {
    double deg = std::clamp(rad * 180.0 / M_PI, -MAX_PAN, MAX_PAN);
    double norm = (MAX_PAN > 0) ? deg / MAX_PAN : 0.0;
    current_pan_ = deg * M_PI / 180.0;
    auto & lim = servo_.limits[ServoController::CH_PAN];
    servo_.apply_angle(ServoController::CH_PAN, lim.from_normalized(norm));
  }

  void apply_tilt(double rad) {
    double deg = std::clamp(rad * 180.0 / M_PI, -MAX_TILT, MAX_TILT);
    double norm = (MAX_TILT > 0) ? deg / MAX_TILT : 0.0;
    current_tilt_ = deg * M_PI / 180.0;
    auto & lim = servo_.limits[ServoController::CH_TILT];
    servo_.apply_angle(ServoController::CH_TILT, lim.from_normalized(-norm));
  }

  // ── Calibration ──

  void load_and_apply_calibration() {
    struct stat st{};
    if (stat(calibration_file_.c_str(), &st) == 0) {
      cal_ = CalibrationData::load_json(calibration_file_);
      RCLCPP_INFO(get_logger(), "Loaded calibration from %s", calibration_file_.c_str());
    } else {
      cal_.steering_center = get_parameter("steering_center").as_double();
      cal_.pan_center = get_parameter("pan_center").as_double();
      cal_.tilt_center = get_parameter("tilt_center").as_double();
      cal_.reverse_direction = get_parameter("reverse_direction").as_bool();
      cal_.speed_gain = get_parameter("speed_gain").as_double();
      cal_.source = "yaml_params";
      RCLCPP_INFO(get_logger(), "Using ROS parameters for calibration");
    }
    apply_calibration();
  }

  void apply_calibration(const std::string & move_to_center = "") {
    double max_servo_off = std::asin(
      std::sin(MAX_STEERING * M_PI / 180.0) / servo_.steering_ratio) * 180.0 / M_PI;

    servo_.set_limits(ServoController::CH_STEERING,
      ServoController::SERVO_CENTER - max_servo_off,
      ServoController::SERVO_CENTER + max_servo_off,
      ServoController::SERVO_CENTER);
    servo_.set_trim(ServoController::CH_STEERING, cal_.steering_center);

    servo_.set_limits(ServoController::CH_THROTTLE,
      ServoController::SERVO_CENTER - 30,
      ServoController::SERVO_CENTER + 30,
      ServoController::SERVO_CENTER);

    servo_.set_limits(ServoController::CH_PAN,
      ServoController::SERVO_CENTER - MAX_PAN,
      ServoController::SERVO_CENTER + MAX_PAN,
      ServoController::SERVO_CENTER);
    servo_.set_trim(ServoController::CH_PAN, cal_.pan_center);

    servo_.set_limits(ServoController::CH_TILT,
      ServoController::SERVO_CENTER - MAX_TILT,
      ServoController::SERVO_CENTER + MAX_TILT,
      ServoController::SERVO_CENTER);
    servo_.set_trim(ServoController::CH_TILT, -cal_.tilt_center);

    if (!move_to_center.empty()) {
      static const std::map<std::string, int> ch_map = {
        {"pan", ServoController::CH_PAN},
        {"tilt", ServoController::CH_TILT},
        {"steering", ServoController::CH_STEERING}};
      auto it = ch_map.find(move_to_center);
      if (it != ch_map.end())
        servo_.apply_angle(it->second, ServoController::SERVO_CENTER);
    }

    servo_.set_inverted(ServoController::CH_THROTTLE, cal_.reverse_direction);
    servo_.speed_gain = cal_.speed_gain;

    RCLCPP_INFO(get_logger(), "Calibration applied (source: %s)", cal_.source.c_str());
    RCLCPP_INFO(get_logger(), "  Steering: max=±%.0f°, center=%.1f°", MAX_STEERING, cal_.steering_center);
    RCLCPP_INFO(get_logger(), "  Speed gain: %.2f, reverse: %s",
      cal_.speed_gain, cal_.reverse_direction ? "true" : "false");

    publish_calibration_status();
  }

  void publish_calibration_status() {
#ifdef HAS_CUSTOM_INTERFACES
    if (!cal_status_pub_) return;
    auto msg = physicar_interfaces::msg::CalibrationStatus();
    msg.header.stamp = now();
    msg.max_steering = MAX_STEERING;
    msg.max_speed = MAX_SPEED;
    msg.max_pan = MAX_PAN;
    msg.max_tilt = MAX_TILT;
    msg.steering_center = cal_.steering_center;
    msg.pan_center = cal_.pan_center;
    msg.tilt_center = cal_.tilt_center;
    msg.reverse_direction = cal_.reverse_direction;
    msg.source = cal_.source;
    msg.is_saved = cal_.is_saved;
    msg.file_path = calibration_file_;
    cal_status_pub_->publish(msg);
#endif
  }

#ifdef HAS_CUSTOM_INTERFACES
  void set_calibration_cb(
    const physicar_interfaces::srv::SetCalibration::Request::SharedPtr req,
    physicar_interfaces::srv::SetCalibration::Response::SharedPtr resp) {

    std::string channel = req->channel;
    std::transform(channel.begin(), channel.end(), channel.begin(), ::tolower);

    auto validate = [](double v, double lo, double hi) { return v >= lo && v <= hi; };

    if (channel == "all") {
      load_and_apply_calibration();
      resp->success = true;
      resp->message = "Calibration reloaded from " + cal_.source;
    } else if (channel == "reverse") {
      cal_.reverse_direction = req->bool_value;
      cal_.is_saved = false;
      apply_calibration();
      resp->success = true;
      resp->message = "reverse_direction set to " + std::string(req->bool_value ? "true" : "false");
    } else if (channel == "speed") {
      resp->success = false;
      resp->message = "max_speed is a hardware constant";
    } else if (channel == "speed_gain") {
      double g = req->max_value;
      if (g < 0.1 || g > 5.0) {
        resp->success = false;
        resp->message = "speed_gain out of range [0.1, 5.0]";
        resp->current_calibration_json = cal_.to_json();
        return;
      }
      cal_.speed_gain = g;
      cal_.is_saved = false;
      apply_calibration();
      resp->success = true;
      resp->message = "speed_gain set to " + std::to_string(g);
    } else if (channel == "steering" || channel == "pan" || channel == "tilt") {
      if (!validate(req->center_value, -15.0, 15.0)) {
        resp->success = false;
        resp->message = channel + "_center out of range [-15, 15]";
        resp->current_calibration_json = cal_.to_json();
        return;
      }
      if (channel == "steering") cal_.steering_center = req->center_value;
      else if (channel == "pan") cal_.pan_center = req->center_value;
      else cal_.tilt_center = req->center_value;
      cal_.is_saved = false;
      apply_calibration(channel);
      resp->success = true;
      resp->message = channel + ": center=" + std::to_string(req->center_value) + "° (moved to center)";
    } else if (channel.size() > 4 && channel.substr(channel.size()-4) == "_max") {
      resp->success = false;
      resp->message = "max values are hardware constants, not adjustable";
    } else if (channel.size() > 7 && channel.substr(channel.size()-7) == "_center") {
      std::string base = channel.substr(0, channel.size()-7);
      if (base == "steering" || base == "pan" || base == "tilt") {
        if (!validate(req->center_value, -15.0, 15.0)) {
          resp->success = false;
          resp->message = base + "_center out of range [-15, 15]";
          resp->current_calibration_json = cal_.to_json();
          return;
        }
        if (base == "steering") cal_.steering_center = req->center_value;
        else if (base == "pan") cal_.pan_center = req->center_value;
        else cal_.tilt_center = req->center_value;
        cal_.is_saved = false;
        apply_calibration(base);
        resp->success = true;
        resp->message = base + ": center=" + std::to_string(req->center_value) + "°";
      } else {
        resp->success = false;
        resp->message = "Invalid channel: " + channel;
        resp->current_calibration_json = cal_.to_json();
        return;
      }
    } else {
      resp->success = false;
      resp->message = "Invalid channel: " + channel;
      resp->current_calibration_json = cal_.to_json();
      return;
    }

    if (cal_.save(calibration_file_))
      resp->message += " (saved)";
    else {
      resp->message += " (save failed!)";
      resp->success = false;
    }
    resp->current_calibration_json = cal_.to_json();
  }

  void get_calibration_cb(
    const physicar_interfaces::srv::GetCalibration::Request::SharedPtr,
    physicar_interfaces::srv::GetCalibration::Response::SharedPtr resp) {
    resp->success = true;
    resp->message = "Current calibration from " + cal_.source;
    resp->max_steering = MAX_STEERING;
    resp->max_speed = MAX_SPEED;
    resp->max_pan = MAX_PAN;
    resp->max_tilt = MAX_TILT;
    resp->steering_center = cal_.steering_center;
    resp->pan_center = cal_.pan_center;
    resp->tilt_center = cal_.tilt_center;
    resp->reverse_direction = cal_.reverse_direction;
    resp->speed_gain = cal_.speed_gain;
    resp->source = cal_.source;
    resp->calibration_json = cal_.to_json();
  }
#endif

  // ── ESC init ──

  void initialize_esc() {
    RCLCPP_INFO(get_logger(), "ESC initialization sequence starting...");
    servo_.center_all();
    std::this_thread::sleep_for(100ms);
    for (int i = 0; i < 50; ++i) {
      servo_.apply_angle(ServoController::CH_THROTTLE, 90);
      std::this_thread::sleep_for(20ms);
    }
    std::this_thread::sleep_for(500ms);
    servo_.center_all();
    RCLCPP_INFO(get_logger(), "ESC initialization complete");
  }

  // ── Publishers ──

  void publish_imu() {
    if (!board_->is_connected()) return;
    auto imu = board_->read_imu();

    auto msg = sensor_msgs::msg::Imu();
    msg.header.stamp = now();
    msg.header.frame_id = imu_frame_id_;

    // Remap IMU chip axes to vehicle body frame (X-fwd, Y-left, Z-up)
    // SDK axes → ROS: x=-sdk_y, y=-sdk_x, z=sdk_z
    msg.linear_acceleration.x = -imu.ay;
    msg.linear_acceleration.y = -imu.ax;
    msg.linear_acceleration.z =  imu.az;
    msg.angular_velocity.x = -imu.gy;
    msg.angular_velocity.y = -imu.gx;
    msg.angular_velocity.z = -imu.gz;
    msg.orientation_covariance[0] = -1.0;
    imu_pub_->publish(msg);

    // Magnetometer
    auto mag = sensor_msgs::msg::MagneticField();
    mag.header = msg.header;
    mag.magnetic_field.x = imu.mx * 1e-6;
    mag.magnetic_field.y = imu.my * 1e-6;
    mag.magnetic_field.z = imu.mz * 1e-6;
    mag_pub_->publish(mag);
  }

  void publish_joint_states() {
    auto msg = sensor_msgs::msg::JointState();
    msg.header.stamp = now();
    msg.name = {
      "front_left_steering_joint", "front_right_steering_joint",
      "camera_pan_joint", "camera_tilt_joint",
      "front_left_wheel_joint", "front_right_wheel_joint",
      "rear_left_wheel_joint", "rear_right_wheel_joint"
    };

    double left_steer = 0, right_steer = 0;
    if (std::abs(current_steering_) > 0.001) {
      double R = wheelbase_ / std::tan(std::abs(current_steering_));
      double inner = std::atan(wheelbase_ / (R - track_width_ / 2));
      double outer = std::atan(wheelbase_ / (R + track_width_ / 2));
      if (current_steering_ > 0) { left_steer = inner; right_steer = outer; }
      else { left_steer = -outer; right_steer = -inner; }
    }

    double wheel_vel = current_throttle_ / wheel_radius_;
    msg.position = {left_steer, right_steer, current_pan_, current_tilt_,
                    0, 0, 0, 0};
    msg.velocity = {0, 0, 0, 0, wheel_vel, wheel_vel, wheel_vel, wheel_vel};
    joint_pub_->publish(msg);
  }

  void publish_battery() {
    if (!board_->is_connected()) return;
    double voltage = board_->read_battery_voltage();

    auto msg = sensor_msgs::msg::BatteryState();
    msg.header.stamp = now();
    msg.header.frame_id = "base_link";
    msg.voltage = voltage;
    msg.design_capacity = 2.0;
    msg.power_supply_technology = sensor_msgs::msg::BatteryState::POWER_SUPPLY_TECHNOLOGY_LIPO;

    if (voltage <= 0) {
      // No serial data yet — publish heartbeat so watchdog doesn't kill us
      msg.present = false;
      msg.percentage = 0.0f;
      msg.power_supply_status = sensor_msgs::msg::BatteryState::POWER_SUPPLY_STATUS_UNKNOWN;
      msg.power_supply_health = sensor_msgs::msg::BatteryState::POWER_SUPPLY_HEALTH_UNKNOWN;
      battery_pub_->publish(msg);
      return;
    }

    if (!battery_cells_detected_) {
      battery_cells_detected_ = true;
      int cells = detect_cells(voltage);
      battery_low_thresh_ = battery_low_threshold(voltage);
      RCLCPP_INFO(get_logger(), "Battery detected: %dS LiPo (%.1fV), low: %.1fV",
        cells, voltage, battery_low_thresh_);
    }
    current_voltage_ = voltage;
    int pct = battery_percentage(voltage);

    msg.percentage = pct / 100.0f;
    msg.present = true;
    msg.power_supply_status = sensor_msgs::msg::BatteryState::POWER_SUPPLY_STATUS_DISCHARGING;

    if (voltage < battery_low_thresh_) {
      msg.power_supply_health = sensor_msgs::msg::BatteryState::POWER_SUPPLY_HEALTH_DEAD;
      if (!battery_low_warned_) {
        RCLCPP_WARN(get_logger(), "LOW BATTERY! %.2fV (%d%%)", voltage, pct);
        battery_low_warned_ = true;
        board_->set_beep(500);
      }
    } else {
      msg.power_supply_health = sensor_msgs::msg::BatteryState::POWER_SUPPLY_HEALTH_GOOD;
      battery_low_warned_ = false;
    }
    battery_pub_->publish(msg);
  }

  // ── Utility ──

  static double steady_now() {
    return std::chrono::duration<double>(
      std::chrono::steady_clock::now().time_since_epoch()).count();
  }

  // ── Members ──

  std::unique_ptr<RosmasterBoard> board_;
  GpioPwmBoard gpio_board_;
  ServoController servo_;
  CalibrationData cal_;

  std::string imu_frame_id_;
  std::string calibration_file_;
  double wheel_radius_ = 0.0375, wheelbase_ = 0.18, track_width_ = 0.16;

  // ESC state
  double target_speed_ = 0, actual_speed_ = 0, speed_adjust_ = 0;
  double actual_speed_time_ = 0, prev_actual_speed_ = 0, prev_odom_time_ = 0;
  double current_voltage_ = 7.4;
  bool brake_active_ = false;

  // Joint state
  double current_steering_ = 0, current_throttle_ = 0;
  double current_pan_ = 0, current_tilt_ = 0;

  // Battery
  double battery_low_thresh_ = 6.6;
  bool battery_low_warned_ = false;
  bool battery_cells_detected_ = false;

  // Publishers
  rclcpp::Publisher<sensor_msgs::msg::Imu>::SharedPtr imu_pub_;
  rclcpp::Publisher<sensor_msgs::msg::MagneticField>::SharedPtr mag_pub_;
  rclcpp::Publisher<sensor_msgs::msg::BatteryState>::SharedPtr battery_pub_;
  rclcpp::Publisher<sensor_msgs::msg::JointState>::SharedPtr joint_pub_;
#ifdef HAS_CUSTOM_INTERFACES
  rclcpp::Publisher<physicar_interfaces::msg::CalibrationStatus>::SharedPtr cal_status_pub_;
  rclcpp::Service<physicar_interfaces::srv::SetCalibration>::SharedPtr set_cal_srv_;
  rclcpp::Service<physicar_interfaces::srv::GetCalibration>::SharedPtr get_cal_srv_;
#endif

  // Timers & subscriptions (prevent destruction — callback groups hold WeakPtr)
  rclcpp::TimerBase::SharedPtr imu_timer_, joint_timer_, battery_timer_;
  std::vector<rclcpp::SubscriptionBase::SharedPtr> subs_;
};

int main(int argc, char ** argv) {
  rclcpp::init(argc, argv);
  auto node = std::make_shared<PhysicarDriverNode>();
  rclcpp::executors::MultiThreadedExecutor exec;
  exec.add_node(node);
  exec.spin();
  rclcpp::shutdown();
  return 0;
}
