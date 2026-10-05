-- --------------------------------------------------------
-- Host:                         127.0.0.1
-- Versi server:                 10.4.32-MariaDB - mariadb.org binary distribution
-- OS Server:                    Win64
-- HeidiSQL Versi:               12.15.0.7171
-- --------------------------------------------------------

/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
/*!40101 SET NAMES utf8 */;
/*!50503 SET NAMES utf8mb4 */;
/*!40103 SET @OLD_TIME_ZONE=@@TIME_ZONE */;
/*!40103 SET TIME_ZONE='+00:00' */;
/*!40014 SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0 */;
/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;
/*!40111 SET @OLD_SQL_NOTES=@@SQL_NOTES, SQL_NOTES=0 */;


-- Membuang struktur basisdata untuk webcctv
CREATE DATABASE IF NOT EXISTS `webcctv` /*!40100 DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci */;
USE `webcctv`;

-- membuang struktur untuk table webcctv.cameras
CREATE TABLE IF NOT EXISTS `cameras` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `name` varchar(100) NOT NULL,
  `mtx_path` varchar(100) DEFAULT NULL,
  `location` varchar(150) DEFAULT NULL,
  `rtsp_url` varchar(500) NOT NULL,
  `nvr_dvr` enum('ipcam','nvr','dvr','youtube') DEFAULT 'ipcam',
  `channel` int(11) DEFAULT 1,
  `codec` enum('h264','h265','auto') DEFAULT 'auto',
  `is_public` tinyint(1) DEFAULT 1,
  `is_active` tinyint(1) DEFAULT 1,
  `lat` decimal(10,7) DEFAULT NULL,
  `lng` decimal(10,7) DEFAULT NULL,
  `youtube_embed` varchar(255) DEFAULT NULL,
  `created_at` timestamp NOT NULL DEFAULT current_timestamp(),
  `dvr` varchar(50) DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uniq_mtx_path` (`mtx_path`)
) ENGINE=InnoDB AUTO_INCREMENT=4 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

-- Membuang data untuk tabel webcctv.cameras: ~3 rows (lebih kurang)
INSERT INTO `cameras` (`id`, `name`, `mtx_path`, `location`, `rtsp_url`, `nvr_dvr`, `channel`, `codec`, `is_public`, `is_active`, `lat`, `lng`, `youtube_embed`, `created_at`, `dvr`) VALUES
	(1, 'Gerbang Utama', 'gerbang-utama-1', 'Pos 1', 'rtsp://admin:12345@192.168.1.100:554/cam/realmonitor?channel=1&subtype=0', 'nvr', 1, 'auto', 1, 1, -6.2088000, 106.8456000, NULL, '2026-09-10 01:39:00', NULL),
	(2, 'Parkir', 'parkir-2', 'Basement', 'rtsp://Rsudjbg:Simrs1038@192.168.22.5:554/cam/realmonitor?channel=1&subtype=0', 'dvr', 1, 'auto', 1, 1, -6.2092000, 106.8460000, NULL, '2026-09-10 01:39:00', 'DVR-01'),
	(3, 'Lobby H.265', 'lobby-h-265-3', 'Lobby', 'rtsp://admin:12345@192.168.1.64:554/h265/ch1/main/av_stream', 'ipcam', 1, 'auto', 1, 1, -6.2085000, 106.8450000, NULL, '2026-09-10 01:39:00', NULL);

-- membuang struktur untuk table webcctv.dvr
CREATE TABLE IF NOT EXISTS `dvr` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `name` varchar(35) NOT NULL,
  `channel` int(11) NOT NULL DEFAULT 0,
  `status` tinyint(4) NOT NULL DEFAULT 0,
  `vendor` varchar(50) DEFAULT NULL,
  `lokasi` varchar(50) DEFAULT NULL,
  `ip` varchar(25) DEFAULT NULL,
  `port` int(11) DEFAULT NULL,
  `username` varchar(50) DEFAULT NULL,
  `password` varchar(80) DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `name` (`name`)
) ENGINE=InnoDB AUTO_INCREMENT=5 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

-- Membuang data untuk tabel webcctv.dvr: ~4 rows (lebih kurang)
INSERT INTO `dvr` (`id`, `name`, `channel`, `status`, `vendor`, `lokasi`, `ip`, `port`, `username`, `password`) VALUES
	(1, 'DVR-01', 32, 1, NULL, 'SATPAM ATAS', '192.168.22.5', 554, 'Rsudjbg', NULL),
	(2, 'DVR-02', 32, 1, NULL, 'SATPAM ATAS', '192.168.22.3', 554, 'Rsudjbg', NULL),
	(3, 'DVR-03', 32, 1, NULL, 'SATPAM BAWAH', '192.168.22.150', 554, 'Rsudjbg', NULL),
	(4, 'DVR-04', 0, 0, NULL, 'SIM ATAS', NULL, NULL, NULL, NULL);

-- membuang struktur untuk table webcctv.records
CREATE TABLE IF NOT EXISTS `records` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `camera_id` int(11) DEFAULT NULL,
  `start_time` datetime DEFAULT NULL,
  `end_time` datetime DEFAULT NULL,
  `file_path` varchar(255) DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `camera_id` (`camera_id`),
  CONSTRAINT `records_ibfk_1` FOREIGN KEY (`camera_id`) REFERENCES `cameras` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

-- Membuang data untuk tabel webcctv.records: ~0 rows (lebih kurang)

-- membuang struktur untuk table webcctv.users
CREATE TABLE IF NOT EXISTS `users` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `username` varchar(50) NOT NULL,
  `password` varchar(255) NOT NULL,
  `role` enum('admin','public') NOT NULL DEFAULT 'public',
  `created_at` timestamp NOT NULL DEFAULT current_timestamp(),
  `email` varchar(50) DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `username` (`username`)
) ENGINE=InnoDB AUTO_INCREMENT=4 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

-- Membuang data untuk tabel webcctv.users: ~3 rows (lebih kurang)
INSERT INTO `users` (`id`, `username`, `password`, `role`, `created_at`, `email`) VALUES
	(1, 'admin@test', 'pbkdf2:sha256:600000$FwyUzGrDkF95uXui$60bf5b71c4f806062577552c80f06c2a6b3b3d17f6ff28ea0b48e56cd8c6fc8c', 'admin', '2026-09-10 01:39:00', NULL),
	(2, 'publik', 'pbkdf2:sha256:600000$FwyUzGrDkF95uXui$60bf5b71c4f806062577552c80f06c2a6b3b3d17f6ff28ea0b48e56cd8c6fc8c', 'public', '2026-09-10 01:39:00', NULL),
	(1, 'aries.setiyawan@gmail.com', 'pbkdf2:sha256:600000$FwyUzGrDkF95uXui$60bf5b71c4f806062577552c80f06c2a6b3b3d17f6ff28ea0b48e56cd8c6fc8c', 'admin', '2026-09-14 03:53:00', NULL);

/*!40103 SET TIME_ZONE=IFNULL(@OLD_TIME_ZONE, 'system') */;
/*!40101 SET SQL_MODE=IFNULL(@OLD_SQL_MODE, '') */;
/*!40014 SET FOREIGN_KEY_CHECKS=IFNULL(@OLD_FOREIGN_KEY_CHECKS, 1) */;
/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;
/*!40111 SET SQL_NOTES=IFNULL(@OLD_SQL_NOTES, 1) */;
