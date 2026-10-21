"""
Meta Ads Hunter - Core Scraper & Media Pipeline
Wraps MetaAdsCollector with clean structured exports, folder management, and media downloading.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from meta_ads_collector import MetaAdsCollector
from meta_ads_collector.filters import FilterConfig
from meta_ads_collector.models import Ad, AdCreative
from tenacity import retry, stop_after_attempt, wait_exponential

from src.relevance import is_ad_relevant

# Ensure stdout handles UTF-8 on Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("MetaAdsHunter")


class MetaAdsHunter:
    """High-level hunter for Meta Ads Library with automatic media download and metadata archiving."""

    def __init__(self, output_dir: str = "downloads") -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.collector = MetaAdsCollector()

    def close(self) -> None:
        """Close collector sessions."""
        try:
            self.collector.close()
        except Exception:
            pass

    def __enter__(self) -> MetaAdsHunter:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def _sanitize_folder_name(self, name: str) -> str:
        """Sanitize string for Windows folder naming."""
        keepchars = (" ", ".", "_", "-")
        clean = "".join(c for c in name if c.isalnum() or c in keepchars).strip()
        return clean or "unknown"

    def _ad_to_dict(self, ad: Ad) -> Dict[str, Any]:
        """Convert Ad dataclass to clean, serializable dictionary."""
        data: Dict[str, Any] = {
            "ad_id": ad.id or ad.ad_library_id,
            "page_id": ad.page.id if ad.page else None,
            "page_name": ad.page.name if ad.page else None,
            "page_url": ad.page.page_url if ad.page else None,
            "profile_picture_url": ad.page.profile_picture_url if ad.page else None,
            "is_active": ad.is_active,
            "ad_status": ad.ad_status,
            "delivery_start_time": (
                ad.delivery_start_time.isoformat()
                if isinstance(ad.delivery_start_time, datetime)
                else str(ad.delivery_start_time) if ad.delivery_start_time else None
            ),
            "delivery_stop_time": (
                ad.delivery_stop_time.isoformat()
                if isinstance(ad.delivery_stop_time, datetime)
                else str(ad.delivery_stop_time) if ad.delivery_stop_time else None
            ),
            "total_active_time": ad.total_active_time,
            "publisher_platforms": ad.publisher_platforms or [],
            "snapshot_url": ad.snapshot_url or ad.ad_snapshot_url,
            "creatives": [],
        }

        # Calculate active days if possible
        if ad.delivery_start_time and isinstance(ad.delivery_start_time, datetime):
            now = datetime.now(timezone.utc)
            start = (
                ad.delivery_start_time
                if ad.delivery_start_time.tzinfo
                else ad.delivery_start_time.replace(tzinfo=timezone.utc)
            )
            data["days_active"] = max(0, (now - start).days)
        else:
            data["days_active"] = 0

        for idx, c in enumerate(ad.creatives or []):
            creative_info = {
                "index": idx,
                "title": c.title,
                "body": c.body,
                "caption": c.caption,
                "description": c.description,
                "link_url": c.link_url,
                "cta_text": c.cta_text,
                "cta_type": c.cta_type,
                "has_video": bool(c.video_hd_url or c.video_url or c.video_sd_url),
                "has_image": bool(c.image_url),
                "video_hd_url": c.video_hd_url,
                "video_sd_url": c.video_sd_url or c.video_url,
                "image_url": c.image_url,
                "thumbnail_url": c.thumbnail_url,
            }
            data["creatives"].append(creative_info)

        return data

    def search_ads(
        self,
        query: str = "",
        country: str = "US",
        media_filter: str = "video",  # 'video', 'image', 'all'
        limit: int = 10,
        min_active_days: int = 0,
    ) -> List[Dict[str, Any]]:
        """Search ads by keyword or brand name."""
        has_video = True if media_filter == "video" else None
        has_image = True if media_filter == "image" else None

        filter_cfg = FilterConfig(
            has_video=has_video,
            has_image=has_image,
        )

        target_country = "US" if country.upper() == "ALL" else country.upper()

        logger.info(
            f"Buscando anuncios: query='{query}', country='{target_country}', media='{media_filter}', limit={limit}"
        )

        raw_results = self.collector.collect(
            query=query,
            country=target_country,
            max_results=limit,
            filter_config=filter_cfg,
        )

        logger.info(f"Encontrados {len(raw_results)} anuncios brutos.")

        parsed_ads: List[Dict[str, Any]] = []
        for ad in raw_results:
            ad_dict = self._ad_to_dict(ad)
            if min_active_days > 0 and ad_dict["days_active"] < min_active_days:
                continue

            # Extract first creative texts for relevance audit
            creatives = ad_dict.get("creatives", [])
            first_c = creatives[0] if creatives else {}
            ad_for_check = {
                "page_name": ad_dict.get("page_name"),
                "headline": first_c.get("title") or "",
                "body_text": first_c.get("body") or "",
                "link_url": first_c.get("link_url") or "",
            }

            relevant, reason = is_ad_relevant(ad_for_check, target_query=query)
            if not relevant:
                logger.warning(f"  [X] Descartado por Jinora (Irrelevante): {ad_dict.get('page_name')} ({ad_dict.get('ad_id')}) -> {reason}")
                continue

            parsed_ads.append(ad_dict)

        # Sort by days active descending (longest running winning ads first)
        parsed_ads.sort(key=lambda x: x.get("days_active", 0), reverse=True)
        return parsed_ads

    def _download_file_streaming(
        self,
        url: str,
        dest_path: Path,
        timeout: int = 120,
        min_bytes: int = 500,
        max_bytes: int = 200 * 1024 * 1024,
    ) -> bool:
        """Download file via streaming into temporary .tmp file and atomic rename on success."""
        tmp_path = dest_path.with_suffix(f"{dest_path.suffix}.tmp")

        @retry(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=2, max=10),
            reraise=True,
        )
        def _fetch_stream() -> int:
            total_downloaded = 0
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

            with self.collector.client.session.get(url, stream=True, timeout=timeout) as resp:
                if resp.status_code != 200:
                    raise IOError(f"HTTP status {resp.status_code} while downloading {url}")

                with open(tmp_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=65536):
                        if not chunk:
                            continue
                        total_downloaded += len(chunk)
                        if total_downloaded > max_bytes:
                            raise ValueError(
                                f"Archivo excede límite de {max_bytes / (1024 * 1024):.0f} MB: {total_downloaded} bytes"
                            )
                        f.write(chunk)

            if total_downloaded < min_bytes:
                raise IOError(f"Archivo descargado demasiado pequeño ({total_downloaded} bytes)")

            # Atomic rename on success
            tmp_path.replace(dest_path)
            return total_downloaded

        try:
            downloaded_bytes = _fetch_stream()
            logger.info(f"  [+] Descargado ({downloaded_bytes / (1024 * 1024):.2f} MB): {dest_path.name}")
            return True
        except Exception as e:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            logger.warning(f"  [!] Falló descarga de {dest_path.name}: {e}")
            return False

    def download_ad(
        self,
        ad_data: Dict[str, Any],
        subfolder: str = "general",
    ) -> Dict[str, Any]:
        """Download ad media via streaming and save metadata.json in target directory.
        Saves relative paths with respect to output_dir.
        """
        ad_id = str(ad_data.get("ad_id"))
        clean_sub = self._sanitize_folder_name(subfolder)
        target_dir = self.output_dir / clean_sub / ad_id
        target_dir.mkdir(parents=True, exist_ok=True)

        downloaded_files: List[str] = []

        # Download media using streaming and retry
        for c in ad_data.get("creatives", []):
            idx = c["index"]

            # Prefer HD Video, fallback to SD (120s timeout, max 200MB)
            video_url = c.get("video_hd_url") or c.get("video_sd_url")
            if video_url:
                v_path = target_dir / f"video_{idx}.mp4"
                if not v_path.exists():
                    success = self._download_file_streaming(
                        url=video_url,
                        dest_path=v_path,
                        timeout=120,
                        min_bytes=1000,
                        max_bytes=200 * 1024 * 1024,
                    )
                    if success:
                        rel_v = v_path.relative_to(self.output_dir).as_posix()
                        downloaded_files.append(rel_v)
                else:
                    rel_v = v_path.relative_to(self.output_dir).as_posix()
                    downloaded_files.append(rel_v)

            # Download Image / Thumbnail (30s timeout)
            img_url = c.get("image_url") or c.get("thumbnail_url")
            if img_url:
                img_path = target_dir / f"image_{idx}.jpg"
                if not img_path.exists():
                    success = self._download_file_streaming(
                        url=img_url,
                        dest_path=img_path,
                        timeout=30,
                        min_bytes=500,
                    )
                    if success:
                        rel_img = img_path.relative_to(self.output_dir).as_posix()
                        downloaded_files.append(rel_img)
                else:
                    rel_img = img_path.relative_to(self.output_dir).as_posix()
                    downloaded_files.append(rel_img)

        # Save metadata.json
        ad_data["downloaded_files"] = downloaded_files
        meta_file = target_dir / "metadata.json"
        meta_file.write_text(json.dumps(ad_data, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info(f"[OK] Metadatos guardados en: {meta_file}")

        return ad_data
