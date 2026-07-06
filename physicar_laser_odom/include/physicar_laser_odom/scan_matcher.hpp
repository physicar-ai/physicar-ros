// Copyright 2026 AICASTLE Inc.
//
// This program is free software: you can redistribute it and/or modify
// it under the terms of the GNU General Public License as published by
// the Free Software Foundation, either version 3 of the License, or
// (at your option) any later version.
//
// This program is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
// GNU General Public License for more details.
//
// You should have received a copy of the GNU General Public License
// along with this program.  If not, see <https://www.gnu.org/licenses/>.

#pragma once

#include <cmath>
#include <cstring>
#include <vector>
#include <algorithm>

#include <Eigen/Core>
#include <Eigen/Geometry>

namespace physicar {

/// A 2D point extracted from a laser scan.
struct Point2D {
  float x, y;
};

/// Pose increment (dx, dy, dyaw).
struct Pose2D {
  double x = 0, y = 0, yaw = 0;

  /// Compose: this ⊕ other
  Pose2D compose(const Pose2D& o) const {
    const double c = std::cos(yaw), s = std::sin(yaw);
    return {x + c * o.x - s * o.y,
            y + s * o.x + c * o.y,
            yaw + o.yaw};
  }

  /// Transform a point by this pose.
  Point2D transform(const Point2D& p) const {
    const float c = static_cast<float>(std::cos(yaw));
    const float s = static_cast<float>(std::sin(yaw));
    return {c * p.x - s * p.y + static_cast<float>(x),
            s * p.x + c * p.y + static_cast<float>(y)};
  }
};

/// Result of a scan match.
struct MatchResult {
  Pose2D delta;           ///< Estimated motion (dx, dy, dyaw)
  double fitness = 1e9;   ///< Mean point-to-line residual (lower = better)
  int iterations = 0;     ///< Iterations used
  bool converged = false; ///< Did the solver converge?
};

// ============================================================================
//  Point-to-Line ICP  (Censi, ICRA 2008 simplified)
// ============================================================================

/// Convert a LaserScan into a vector of 2D points (skip NaN/Inf).
inline std::vector<Point2D> scan_to_points(
    const float* ranges, int count,
    float angle_min, float angle_inc,
    float range_min, float range_max)
{
  std::vector<Point2D> pts;
  pts.reserve(count);
  for (int i = 0; i < count; ++i) {
    const float r = ranges[i];
    if (!std::isfinite(r) || r < range_min || r > range_max) continue;
    const float a = angle_min + static_cast<float>(i) * angle_inc;
    pts.push_back(Point2D{r * std::cos(a), r * std::sin(a)});
  }
  return pts;
}

/// Point-to-Line ICP scan matcher.
///
/// Matches `source` (current scan) against `target` (previous scan)
/// to estimate the rigid-body motion between them.
///
/// Algorithm:
///   1. For each source point, find nearest target point.
///   2. Compute the local line direction from the target point's neighbors.
///   3. Minimize the point-to-line distance using the closed-form
///      Gauss-Newton update on SE(2).
///   4. Iterate until convergence or max iterations.
///
class ScanMatcher {
 public:
  // Parameters
  int max_iterations = 30;
  double convergence_threshold = 1e-5;  // sum of |dx|+|dy|+|dyaw|
  double max_correspondence_dist = 0.5; // meters
  double outlier_ratio = 0.75;          // Cauchy kernel scale factor

  /// Match source scan to target scan.
  /// Returns the estimated rigid transform (dx, dy, dyaw) that moves
  /// the source scan into the target frame.
  MatchResult match(const std::vector<Point2D>& target,
                    const std::vector<Point2D>& source,
                    const Pose2D& initial_guess = {}) const
  {
    if (target.size() < 10 || source.size() < 10)
      return {};

    // Build grid index for target points (O(N) construction, O(1) lookup)
    GridIndex grid(target, static_cast<float>(max_correspondence_dist));

    MatchResult result;
    Pose2D pose = initial_guess;

    for (int iter = 0; iter < max_iterations; ++iter) {
      // Transform source points by current estimate
      std::vector<Point2D> transformed(source.size());
      for (size_t i = 0; i < source.size(); ++i)
        transformed[i] = pose.transform(source[i]);

      // Build correspondences + point-to-line system
      // H * delta = b   (3x3 system)
      Eigen::Matrix3d H = Eigen::Matrix3d::Zero();
      Eigen::Vector3d b = Eigen::Vector3d::Zero();
      int n_corr = 0;
      double residual_sum = 0;

      const float max_dist2 = static_cast<float>(
          max_correspondence_dist * max_correspondence_dist);

      for (size_t i = 0; i < transformed.size(); ++i) {
        const auto& sp = transformed[i];

        // Find nearest neighbor via grid lookup — O(1) per query
        int best_idx = -1;
        float best_dist2 = max_dist2;
        grid.find_nearest(sp, best_idx, best_dist2);

        if (best_idx < 0) continue;

        // Compute local line normal from target neighbors
        Eigen::Vector2d normal;
        if (!compute_normal(target, best_idx, normal)) continue;

        // Point-to-line residual: n^T * (sp - tp)
        const double ex = static_cast<double>(sp.x) - target[best_idx].x;
        const double ey = static_cast<double>(sp.y) - target[best_idx].y;
        const double residual = normal.x() * ex + normal.y() * ey;

        // Cauchy robust kernel weight
        const double scale = outlier_ratio * std::sqrt(best_dist2);
        const double w = 1.0 / (1.0 + (residual * residual) / (scale * scale + 1e-9));

        // Jacobian: d(residual)/d(dx, dy, dyaw)
        const double sx = source[i].x, sy = source[i].y;
        const double c = std::cos(pose.yaw), s_yaw = std::sin(pose.yaw);
        const double dyaw_term = normal.x() * (-s_yaw * sx - c * sy)
                               + normal.y() * (c * sx - s_yaw * sy);

        Eigen::Vector3d J;
        J << normal.x(), normal.y(), dyaw_term;

        // Accumulate Gauss-Newton
        H += w * J * J.transpose();
        b += w * residual * J;

        residual_sum += std::abs(residual);
        ++n_corr;
      }

      if (n_corr < 6) {
        result.converged = false;
        result.iterations = iter;
        return result;
      }

      // Solve H * delta = -b
      Eigen::Vector3d delta = H.ldlt().solve(-b);

      // Update pose
      pose.x   += delta(0);
      pose.y   += delta(1);
      pose.yaw += delta(2);

      result.fitness = residual_sum / n_corr;
      result.iterations = iter + 1;

      // Check convergence
      if (std::abs(delta(0)) + std::abs(delta(1)) + std::abs(delta(2))
          < convergence_threshold) {
        result.converged = true;
        break;
      }
    }

    result.delta = pose;
    return result;
  }

 private:
  // ── Grid-based spatial index for O(1) nearest-neighbor lookup ──
  struct GridIndex {
    float cell_size;
    float inv_cell;
    float ox, oy;  // origin offset
    int nx, ny;     // grid dimensions
    std::vector<std::vector<int>> cells;
    const std::vector<Point2D>* pts;

    GridIndex(const std::vector<Point2D>& points, float max_dist) : pts(&points) {
      cell_size = max_dist;  // one cell = max search radius
      inv_cell = 1.0f / cell_size;

      // Find bounding box
      float minx = 1e9f, miny = 1e9f, maxx = -1e9f, maxy = -1e9f;
      for (const auto& p : points) {
        minx = std::min(minx, p.x); maxx = std::max(maxx, p.x);
        miny = std::min(miny, p.y); maxy = std::max(maxy, p.y);
      }
      ox = minx - cell_size;
      oy = miny - cell_size;
      nx = static_cast<int>((maxx - ox) * inv_cell) + 2;
      ny = static_cast<int>((maxy - oy) * inv_cell) + 2;

      cells.resize(nx * ny);
      for (size_t i = 0; i < points.size(); ++i) {
        int ci = static_cast<int>((points[i].x - ox) * inv_cell);
        int cj = static_cast<int>((points[i].y - oy) * inv_cell);
        cells[ci * ny + cj].push_back(static_cast<int>(i));
      }
    }

    void find_nearest(const Point2D& query, int& best_idx, float& best_dist2) const {
      int ci = static_cast<int>((query.x - ox) * inv_cell);
      int cj = static_cast<int>((query.y - oy) * inv_cell);

      // Search 3x3 neighborhood
      for (int di = -1; di <= 1; ++di) {
        int ri = ci + di;
        if (ri < 0 || ri >= nx) continue;
        for (int dj = -1; dj <= 1; ++dj) {
          int rj = cj + dj;
          if (rj < 0 || rj >= ny) continue;
          for (int idx : cells[ri * ny + rj]) {
            const float dx = query.x - (*pts)[idx].x;
            const float dy = query.y - (*pts)[idx].y;
            const float d2 = dx * dx + dy * dy;
            if (d2 < best_dist2) {
              best_dist2 = d2;
              best_idx = idx;
            }
          }
        }
      }
    }
  };

  /// Compute normal from target point's neighbors.
  /// Returns false if neighbors don't exist or are too far apart.
  static bool compute_normal(const std::vector<Point2D>& pts, int idx,
                             Eigen::Vector2d& normal) {
    if (idx <= 0 || idx >= static_cast<int>(pts.size()) - 1)
      return false;

    const auto& p0 = pts[idx - 1];
    const auto& p1 = pts[idx + 1];

    double dx = p1.x - p0.x;
    double dy = p1.y - p0.y;
    double len = std::sqrt(dx * dx + dy * dy);

    if (len < 1e-6) return false;
    if (len > 0.5) return false;

    normal.x() = -dy / len;
    normal.y() =  dx / len;
    return true;
  }
};

}  // namespace physicar
