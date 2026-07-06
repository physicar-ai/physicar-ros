# CalibrationStatus.msg
# Published when calibration changes
#
# Topic: /physicar_driver/calibration_status

std_msgs/Header header

# Max values
float64 max_steering       # ± degrees (wheel angle)
float64 max_speed          # ± m/s
float64 max_pan            # ± degrees
float64 max_tilt           # ± degrees

# Center offsets (degrees from 90°)
float64 steering_center
float64 pan_center
float64 tilt_center

# ESC direction
bool reverse_direction   # true: 90° above = forward (reversed ESC)

# Source and status
string source           # 'json_file', 'yaml_params', or 'defaults'
bool is_saved           # true if current values are saved to file
string file_path        # Path to calibration file
