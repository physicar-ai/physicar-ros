#!/usr/bin/env python3
"""
PhysiCar minimum-speed measurement tool (monitoring only)

Usage:
1. Terminal 1: ros2 launch physicar_driver robot.launch.py
2. Terminal 2: python3 measure_min_speed.py
3. Terminal 3: ros2 run physicar_teleop teleop_keyboard

While driving via teleop, automatically captures the speed right before stopping.
"""

import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist
from nav_msgs.msg import Odometry
import time
import os


class MinSpeedMonitor(Node):
    def __init__(self):
        super().__init__('min_speed_monitor')
        
        self.create_subscription(Twist, '/cmd_vel', self.cmd_callback, 10)
        self.create_subscription(Odometry, '/odom', self.odom_callback, 10)
        
        self.cmd_vel = 0.0
        self.prev_cmd_vel = 0.0
        self.actual_speed = 0.0
        
        # For capturing right before stopping
        self.last_cmd_before_stop = 0.0
        self.last_speed_before_stop = 0.0
        
        # Measurement log (captures recorded right before stopping)
        self.forward_captures = []  # [(cmd_vel, actual_speed), ...]
        self.reverse_captures = []
        
        # Display timer
        self.create_timer(0.2, self.display)
        
    def cmd_callback(self, msg: Twist):
        self.prev_cmd_vel = self.cmd_vel
        self.cmd_vel = msg.linear.x
        
        # Detect stop command (was moving, now zero)
        if abs(self.prev_cmd_vel) > 0.01 and abs(self.cmd_vel) < 0.01:
            # Capture value right before stopping
            if abs(self.last_speed_before_stop) > 0.1:
                capture = (self.last_cmd_before_stop, self.last_speed_before_stop)
                
                if self.last_cmd_before_stop > 0:
                    self.forward_captures.append(capture)
                else:
                    self.reverse_captures.append(capture)
        
    def odom_callback(self, msg: Odometry):
        self.actual_speed = msg.twist.twist.linear.x
        
        # Save last value while moving
        if abs(self.cmd_vel) > 0.01:
            self.last_cmd_before_stop = self.cmd_vel
            self.last_speed_before_stop = self.actual_speed
    
    def display(self):
        os.system('clear' if os.name == 'posix' else 'cls')
        
        print("="*60)
        print("  PhysiCar speed measurement (auto-capture on stop)")
        print("="*60)
        print("\n  Drive via teleop, then press 's' or Space to stop!")
        print("  The speed at the moment of stopping is recorded automatically.")
        print("-"*60)
        
        # Current state
        direction = ""
        if self.cmd_vel > 0.01:
            direction = "forward ▶"
        elif self.cmd_vel < -0.01:
            direction = "◀ reverse"
        else:
            direction = "stopped"
        
        print(f"\n  ┌─────────────────┬─────────────────┐")
        print(f"  │  cmd_vel cmd    │  {self.cmd_vel:+.3f} m/s  {direction:>6} │")
        print(f"  ├─────────────────┼─────────────────┤")
        print(f"  │  actual speed   │  {self.actual_speed:+.3f} m/s      │")
        print(f"  └─────────────────┴─────────────────┘")
        
        # Captured measurements
        print("\n" + "-"*60)
        print("  📸 Captured measurements (just before stop)")
        print("-"*60)
        
        if self.forward_captures:
            print(f"\n  Forward ({len(self.forward_captures)} measurements)")
            print("  ┌──────┬────────────────┬────────────────┬──────────┐")
            print("  │  #   │  cmd_vel cmd   │  actual speed  │  ratio   │")
            print("  ├──────┼────────────────┼────────────────┼──────────┤")
            for i, (cmd, actual) in enumerate(self.forward_captures[-5:], 1):
                ratio = actual / cmd if cmd != 0 else 0
                print(f"  │  {i:2}  │  {cmd:+.3f} m/s    │  {actual:+.3f} m/s    │  {ratio:.1f}x    │")
            print("  └──────┴────────────────┴────────────────┴──────────┘")
            
            # Average
            avg_cmd = sum(c[0] for c in self.forward_captures) / len(self.forward_captures)
            avg_actual = sum(c[1] for c in self.forward_captures) / len(self.forward_captures)
            avg_ratio = avg_actual / avg_cmd if avg_cmd != 0 else 0
            print(f"  Average: cmd={avg_cmd:.3f} → actual={avg_actual:.3f} ({avg_ratio:.1f}x)")
        else:
            print("\n  Forward: no captures yet")
            
        if self.reverse_captures:
            print(f"\n  Reverse ({len(self.reverse_captures)} measurements)")
            print("  ┌──────┬────────────────┬────────────────┬──────────┐")
            print("  │  #   │  cmd_vel cmd   │  actual speed  │  ratio   │")
            print("  ├──────┼────────────────┼────────────────┼──────────┤")
            for i, (cmd, actual) in enumerate(self.reverse_captures[-5:], 1):
                ratio = abs(actual / cmd) if cmd != 0 else 0
                print(f"  │  {i:2}  │  {cmd:+.3f} m/s    │  {actual:+.3f} m/s    │  {ratio:.1f}x    │")
            print("  └──────┴────────────────┴────────────────┴──────────┘")
            
            # Average
            avg_cmd = sum(c[0] for c in self.reverse_captures) / len(self.reverse_captures)
            avg_actual = sum(c[1] for c in self.reverse_captures) / len(self.reverse_captures)
            avg_ratio = abs(avg_actual / avg_cmd) if avg_cmd != 0 else 0
            print(f"  Average: cmd={avg_cmd:.3f} → actual={avg_actual:.3f} ({avg_ratio:.1f}x)")
        else:
            print("\n  Reverse: no captures yet")
        
        print("\n" + "-"*60)
        print("  [r] reset captures  [q] quit")
        print("-"*60)


def main():
    rclpy.init()
    node = MinSpeedMonitor()
    
    import threading
    import sys
    import termios
    import tty
    
    running = True
    
    def spin_thread():
        while running:
            rclpy.spin_once(node, timeout_sec=0.1)
    
    thread = threading.Thread(target=spin_thread)
    thread.start()
    
    # Key input handling
    old_settings = termios.tcgetattr(sys.stdin)
    try:
        tty.setcbreak(sys.stdin.fileno())
        while True:
            if sys.stdin in __import__('select').select([sys.stdin], [], [], 0.1)[0]:
                key = sys.stdin.read(1)
                if key == 'q':
                    break
                elif key == 'r':
                    node.forward_captures = []
                    node.reverse_captures = []
    except KeyboardInterrupt:
        pass
    finally:
        running = False
        thread.join()
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
        
        # Print final results
        print("\n\n" + "="*60)
        print("  Final measurement results")
        print("="*60)
        
        if node.forward_captures:
            avg_cmd = sum(c[0] for c in node.forward_captures) / len(node.forward_captures)
            avg_actual = sum(c[1] for c in node.forward_captures) / len(node.forward_captures)
            ratio = avg_actual / avg_cmd if avg_cmd != 0 else 0
            print(f"\n  Forward: cmd={avg_cmd:.3f} → actual={avg_actual:.3f} m/s ({ratio:.1f}x)")
        
        if node.reverse_captures:
            avg_cmd = sum(c[0] for c in node.reverse_captures) / len(node.reverse_captures)
            avg_actual = sum(c[1] for c in node.reverse_captures) / len(node.reverse_captures)
            ratio = abs(avg_actual / avg_cmd) if avg_cmd != 0 else 0
            print(f"  Reverse: cmd={avg_cmd:.3f} → actual={avg_actual:.3f} m/s ({ratio:.1f}x)")
        
        print("\n")
        
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
