#!/usr/bin/env python3
#
# Copyright 2026 AICASTLE Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Camera Processing Node

Camera post-processing pipeline (distortion correction, colour correction, etc.).

Subscribes to /camera/image_raw (sensor_msgs/Image)
Publishes /camera/compressed (sensor_msgs/CompressedImage)

Parameters:
  k1, k2, p1, p2, k3 : OpenCV distortion coefficients (defaults: OV5647 wide-angle estimates)
  alpha               : 0.0=max crop (no black borders), 1.0=no crop (default 0.0)
  jpeg_quality        : JPEG quality (default 70)

Defaults are estimates for the OV5647 wide-angle lens. Accurate values can be
obtained via calibration.
"""

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
from rcl_interfaces.msg import SetParametersResult

from sensor_msgs.msg import Image, CompressedImage

import numpy as np
import cv2


class CameraProcNode(Node):
    def __init__(self):
        super().__init__('camera_proc')

        # Distortion coefficients (calibration result)
        self.declare_parameter('k1', -0.3675)
        self.declare_parameter('k2', 0.1717)
        self.declare_parameter('p1', -0.0021)
        self.declare_parameter('p2', -0.0009)
        self.declare_parameter('k3', -0.0445)
        self.declare_parameter('jpeg_quality', 70)
        # Camera matrix from calibration
        self.declare_parameter('fx', 290.92)
        self.declare_parameter('fy', 290.39)
        self.declare_parameter('cx', 234.47)
        self.declare_parameter('cy', 172.02)
        # Distortion-correction strength: 0.0=off (original FOV), 0.7=98° (recommended), 1.0=full correction (79°)
        self.declare_parameter('dist_scale', 0.7)
        # Output resolution (0 = no resize)
        self.declare_parameter('out_width', 0)
        self.declare_parameter('out_height', 0)

        self._map1 = None
        self._map2 = None
        self._last_shape = None

        # Rebuild map when distortion-related parameters change
        self.add_on_set_parameters_callback(self._on_params_changed)

        qos = QoSProfile(
            depth=1,
            reliability=ReliabilityPolicy.BEST_EFFORT,
            history=HistoryPolicy.KEEP_LAST,
        )

        self._pub = self.create_publisher(CompressedImage, '/camera/compressed', qos)
        self._sub = self.create_subscription(Image, '/camera/image_raw', self._callback, qos)

        self.get_logger().info('Camera proc node started')

    def _on_params_changed(self, params):
        """Invalidate map on distortion-coefficient parameter change → rebuild on next frame"""
        distort_params = {'k1', 'k2', 'p1', 'p2', 'k3', 'fx', 'fy', 'cx', 'cy', 'dist_scale'}
        if any(p.name in distort_params for p in params):
            self._last_shape = None
            self.get_logger().info('Distortion params changed, rebuilding map...')
        return SetParametersResult(successful=True)

    def _build_maps(self, h: int, w: int):
        """Build undistortion maps (rebuild on resolution change)"""
        k1 = self.get_parameter('k1').value
        k2 = self.get_parameter('k2').value
        p1 = self.get_parameter('p1').value
        p2 = self.get_parameter('p2').value
        k3 = self.get_parameter('k3').value
        scale = self.get_parameter('dist_scale').value
        k1, k2, p1, p2, k3 = k1*scale, k2*scale, p1*scale, p2*scale, k3*scale

        # Camera intrinsic matrix
        fx = self.get_parameter('fx').value
        fy = self.get_parameter('fy').value
        cx = self.get_parameter('cx').value
        cy = self.get_parameter('cy').value

        camera_matrix = np.array([
            [fx,  0.0, cx],
            [0.0, fy,  cy],
            [0.0, 0.0, 1.0],
        ], dtype=np.float64)

        dist_coeffs = np.array([k1, k2, p1, p2, k3], dtype=np.float64)

        new_camera_matrix, _ = cv2.getOptimalNewCameraMatrix(
            camera_matrix, dist_coeffs, (w, h), 0.0, (w, h)
        )
        # Preserve aspect ratio: unify to the larger of fx/fy → guarantees no black borders
        f_safe = max(new_camera_matrix[0, 0], new_camera_matrix[1, 1])
        new_camera_matrix[0, 0] = f_safe
        new_camera_matrix[1, 1] = f_safe

        self._map1, self._map2 = cv2.initUndistortRectifyMap(
            camera_matrix, dist_coeffs, None, new_camera_matrix, (w, h), cv2.CV_16SC2
        )
        self._last_shape = (h, w)
        self.get_logger().info(
            f'Undistort map built: {w}x{h}, '
            f'k1={k1:.3f} k2={k2:.3f} p1={p1:.3f} p2={p2:.3f} k3={k3:.3f} scale={scale:.2f}'
        )

    def _callback(self, msg: Image):
        enc = msg.encoding.lower()

        raw = np.frombuffer(msg.data, dtype=np.uint8).reshape(msg.height, msg.step)
        bytes_per_pixel = 3 if enc in ('rgb8', 'bgr8') else 4
        img = raw[:, :msg.width * bytes_per_pixel].reshape(msg.height, msg.width, bytes_per_pixel)

        if bytes_per_pixel == 4:
            img = img[:, :, :3].copy()
        else:
            img = img.copy()

        if enc == 'rgb8':
            img = img[:, :, ::-1]  # RGB → BGR

        # Rebuild map if resolution changed or this is the first frame
        if self._last_shape != (msg.height, msg.width):
            self._build_maps(msg.height, msg.width)

        # Distortion correction
        undistorted = cv2.remap(img, self._map1, self._map2, cv2.INTER_LINEAR)

        # Output-resolution resize (when configured)
        out_w = self.get_parameter('out_width').value
        out_h = self.get_parameter('out_height').value
        if out_w > 0 and out_h > 0 and (undistorted.shape[1] != out_w or undistorted.shape[0] != out_h):
            undistorted = cv2.resize(undistorted, (out_w, out_h), interpolation=cv2.INTER_AREA)

        quality = self.get_parameter('jpeg_quality').value
        ok, buf = cv2.imencode('.jpg', undistorted, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            return

        out = CompressedImage()
        out.header = msg.header
        out.format = 'jpeg'
        out.data = buf.tobytes()
        self._pub.publish(out)


def main(args=None):
    rclpy.init(args=args)
    node = CameraProcNode()
    rclpy.spin(node)
    rclpy.shutdown()


if __name__ == '__main__':
    main()
