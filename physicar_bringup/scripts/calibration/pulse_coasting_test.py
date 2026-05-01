#!/usr/bin/env python3
"""
Pulse-start + coasting drive test

Goal: verify whether a short start pulse enables smooth coasting
- Test various pulse durations (0.05s, 0.1s, 0.15s)
- Observe how natural the coasting feels

Controls:
- 1: 0.05s pulse test
- 2: 0.10s pulse test  
- 3: 0.15s pulse test
- c: continuous pulse test (auto pulse every 1s)
- SPACE: emergency stop
- q: quit
"""

import sys
import time
import termios
import tty
import select
import os

# Add parent package to path for direct script execution
script_dir = os.path.dirname(os.path.abspath(__file__))
pkg_dir = os.path.dirname(os.path.dirname(script_dir))
sys.path.insert(0, pkg_dir)

from physicar_bringup.yahboom_board import YahboomBoard


class PulseCoastingTester:
    def __init__(self):
        self.board = YahboomBoard(port='/dev/ttyUSB0', baudrate=115200)
        if not self.board.connect():
            print("❌ Board connection failed!")
            sys.exit(1)
        print("✅ Board connected")
        
        # Forward angle settings
        self.CENTER = 90.0
        self.START_ANGLE = 82.0   # Forward start angle
        self.COAST_ANGLE = 83.0   # Forward coast-hold angle
        
        # Reverse angle settings
        self.START_ANGLE_REV = 98.0   # Reverse start angle
        self.COAST_ANGLE_REV = 97.0   # Reverse coast-hold angle
        
        # Angles for duty-cycle test
        self.MAX_ANGLE = 81.0         # Forward max-speed angle
        self.LOW_ANGLE = 86.0         # Forward low value (closer to 90° than coast)
        self.MAX_ANGLE_REV = 99.0     # Reverse max-speed angle
        self.LOW_ANGLE_REV = 94.0     # Reverse low value (closer to 90° than coast)
        
    def set_esc(self, angle: float):
        self.board.set_servo(1, angle)
        
    def stop(self):
        self.set_esc(self.CENTER)
        print(f"\n🛑 stopped! (90°)")
        
    def check_space(self) -> bool:
        if select.select([sys.stdin], [], [], 0)[0]:
            key = sys.stdin.read(1)
            if key == ' ':
                return True
        return False
        
    def test_single_pulse(self, pulse_duration: float, reverse: bool = False):
        """Single-pulse test (forward/reverse)"""
        direction = "reverse" if reverse else "forward"
        start_angle = self.START_ANGLE_REV if reverse else self.START_ANGLE
        coast_angle = self.COAST_ANGLE_REV if reverse else self.COAST_ANGLE
        
        print(f"\n{'='*50}")
        print(f"🔧 {direction} pulse-start test: {pulse_duration}s")
        print(f"{'='*50}")
        
        # 1. Pulse start
        print(f"⚡ Start pulse: {start_angle}° ({pulse_duration}s)")
        self.set_esc(start_angle)
        
        start_time = time.time()
        while time.time() - start_time < pulse_duration:
            if self.check_space():
                self.stop()
                return
            time.sleep(0.01)
        
        # 2. Coasting
        print(f"🌊 Coasting: {coast_angle}° (observe for 5s)")
        self.set_esc(coast_angle)
        
        start_time = time.time()
        while time.time() - start_time < 5.0:
            elapsed = time.time() - start_time
            print(f"\r   {elapsed:.1f}s - is speed steady? (SPACE=stop)", end='', flush=True)
            if self.check_space():
                self.stop()
                return
            time.sleep(0.1)
        
        self.stop()
        print(f"\n✅ Test complete!")
        print(f"   → Did the {pulse_duration}s pulse give smooth coasting?")
        
    def test_continuous_pulse(self, reverse: bool = False, fast: bool = False):
        """Duty-cycle test — alternate max pulse → low value"""
        direction = "reverse" if reverse else "forward"
        max_angle = self.MAX_ANGLE_REV if reverse else self.MAX_ANGLE
        low_angle = self.LOW_ANGLE_REV if reverse else self.LOW_ANGLE
        
        if fast:
            # v/V: 0.01s period (0.01s max + 0.01s low)
            pulse_duration = 0.01
            low_duration = 0.01
            period_desc = "0.01s max + 0.01s low (50Hz)"
        else:
            # c/C: 0.05s period (0.05s max + 0.05s low)
            pulse_duration = 0.05
            low_duration = 0.05
            period_desc = "0.05s max + 0.05s low (10Hz)"
        
        print(f"\n{'='*50}")
        print(f"🔁 {direction} duty-cycle test")
        print(f"   {period_desc}")
        print(f"   max={max_angle}°, low={low_angle}°")
        print(f"   Press SPACE to stop")
        print(f"{'='*50}")
        
        pulse_count = 0
        while True:
            # Max pulse
            pulse_count += 1
            if pulse_count % 50 == 0:  # print every 50 cycles
                print(f"\r⚡ cycle #{pulse_count} ", end='', flush=True)
            self.set_esc(max_angle)
            
            start_time = time.time()
            while time.time() - start_time < pulse_duration:
                if self.check_space():
                    self.stop()
                    return
                time.sleep(0.001)
            
            # Low value
            self.set_esc(low_angle)
            
            start_time = time.time()
            while time.time() - start_time < low_duration:
                if self.check_space():
                    self.stop()
                    return
                time.sleep(0.001)
                
    def get_key(self) -> str:
        return sys.stdin.read(1)
        
    def run(self):
        old_settings = termios.tcgetattr(sys.stdin)
        try:
            tty.setraw(sys.stdin.fileno())
            self.stop()
            
            print("\n" + "="*50)
            print("🔧 Pulse coasting test")
            print("="*50)
            print(f"Forward: start={self.START_ANGLE}° coast={self.COAST_ANGLE}°")
            print(f"Reverse: start={self.START_ANGLE_REV}° coast={self.COAST_ANGLE_REV}°")
            print(f"Duty: forward max={self.MAX_ANGLE}° low={self.LOW_ANGLE}°")
            print(f"      reverse max={self.MAX_ANGLE_REV}° low={self.LOW_ANGLE_REV}°")
            print("-"*50)
            print("[Forward tests]")
            print("0: 0.01s pulse | 9: 0.005s")
            print("1: 0.05s pulse | 2: 0.10s | 3: 0.15s")
            print("-"*50)
            print("[Reverse tests] (Shift + digit)")
            print("): 0.01s pulse | (: 0.005s")
            print("!: 0.05s pulse | @: 0.10s | #: 0.15s")
            print("-"*50)
            print("[Duty cycle] max→low repeat")
            print("c: 0.01s period 50Hz (forward) | C: (reverse)")
            print("v: 0.05s period 10Hz (forward) | V: (reverse)")
            print("SPACE: emergency stop | q: quit")
            print("="*50)
            
            while True:
                key = self.get_key()
                
                # Forward tests
                if key == '0':
                    self.test_single_pulse(0.01, reverse=False)
                elif key == '9':
                    self.test_single_pulse(0.005, reverse=False)
                elif key == '1':
                    self.test_single_pulse(0.05, reverse=False)
                elif key == '2':
                    self.test_single_pulse(0.10, reverse=False)
                elif key == '3':
                    self.test_single_pulse(0.15, reverse=False)
                # Reverse tests (Shift + digit)
                elif key == ')':  # Shift+0
                    self.test_single_pulse(0.01, reverse=True)
                elif key == '(':  # Shift+9
                    self.test_single_pulse(0.005, reverse=True)
                elif key == '!':  # Shift+1
                    self.test_single_pulse(0.05, reverse=True)
                elif key == '@':  # Shift+2
                    self.test_single_pulse(0.10, reverse=True)
                elif key == '#':  # Shift+3
                    self.test_single_pulse(0.15, reverse=True)
                # Continuous pulse (0.1s period)
                elif key == 'c':
                    self.test_continuous_pulse(reverse=False, fast=False)
                elif key == 'C':  # Shift+C = reverse continuous pulse
                    self.test_continuous_pulse(reverse=True, fast=False)
                # Continuous pulse (0.05s period - fast)
                elif key == 'v':
                    self.test_continuous_pulse(reverse=False, fast=True)
                elif key == 'V':  # Shift+V = reverse fast continuous pulse
                    self.test_continuous_pulse(reverse=True, fast=True)
                elif key == ' ':
                    self.stop()
                elif key == 'q' or key == '\x03':
                    self.stop()
                    print("\n👋 quit")
                    break
                    
        finally:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
            self.stop()
            self.board.disconnect()


if __name__ == '__main__':
    tester = PulseCoastingTester()
    tester.run()
