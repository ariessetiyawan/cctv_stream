-- =========================================================
-- Tabel pivot: kamera ↔ role
-- =========================================================
CREATE TABLE IF NOT EXISTS `camera_access` (
  `id`         INT(11) NOT NULL AUTO_INCREMENT,
  `camera_id`  INT(11) NOT NULL,
  `role`       ENUM('admin','supervisor','public') NOT NULL,
  `created_at` TIMESTAMP NOT NULL DEFAULT current_timestamp(),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uniq_cam_role` (`camera_id`,`role`),
  KEY `idx_role` (`role`),
  CONSTRAINT `camera_access_ibfk_1`
    FOREIGN KEY (`camera_id`) REFERENCES `cameras`(`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

-- =========================================================
-- Backfill dari kolom `is_public` lama
--   is_public=1 → admin, supervisor, public
--   is_public=0 → admin, supervisor
-- =========================================================
INSERT IGNORE INTO camera_access (camera_id, role)
SELECT id, 'admin' FROM cameras;

INSERT IGNORE INTO camera_access (camera_id, role)
SELECT id, 'supervisor' FROM cameras;

INSERT IGNORE INTO camera_access (camera_id, role)
SELECT id, 'public' FROM cameras WHERE is_public = 1;