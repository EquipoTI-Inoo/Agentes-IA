"""
Jinora - Path Normalizer Utility
Converts absolute or machine-specific file paths in data/meta_ads.db to paths
relative to DOWNLOADS_DIR (e.g., 'C:\\Users\\Gabo\\...\\downloads\\Brand\\ad_id\\video_0.mp4' -> 'Brand/ad_id/video_0.mp4').
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Optional

# Ensure UTF-8 output on Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "meta_ads.db"


def to_relative_downloads_path(path_str: Optional[str]) -> Optional[str]:
    """Extract path relative to downloads/ directory with POSIX forward slashes."""
    if not path_str:
        return None
    normalized = str(path_str).replace("\\", "/").strip()
    if "/downloads/" in normalized:
        return normalized.split("/downloads/", 1)[1]
    if normalized.startswith("downloads/"):
        return normalized[len("downloads/"):]
    return normalized


def run_fix_paths(apply_changes: bool = False, db_path: Path = DB_PATH) -> int:
    if not db_path.exists():
        print(f"❌ Error: Database not found at {db_path}")
        return 1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("SELECT id, video_path, image_path FROM ads")
    rows = cur.fetchall()

    print(f"🔍 Analizando {len(rows)} anuncios en: {db_path.name}")
    print(f"⚙️ Modo: {'[APLICAR CAMBIOS]' if apply_changes else '[DRY-RUN (Solo Lectura)]'}\n")

    changed_count = 0
    updates_to_perform = []

    for row in rows:
        ad_id = row["id"]
        old_v = row["video_path"]
        old_i = row["image_path"]

        new_v = to_relative_downloads_path(old_v)
        new_i = to_relative_downloads_path(old_i)

        if new_v != old_v or new_i != old_i:
            changed_count += 1
            updates_to_perform.append((new_v, new_i, ad_id))
            if changed_count <= 5:
                print(f"  [Ad ID: {ad_id}]")
                if new_v != old_v:
                    print(f"    Video: '{old_v}'\n       -> '{new_v}'")
                if new_i != old_i:
                    print(f"    Image: '{old_i}'\n       -> '{new_i}'")

    if changed_count > 5:
        print(f"  ... y {changed_count - 5} filas más con rutas para normalizar.")

    print("\n" + "=" * 50)
    print(f"📊 Total de filas que cambiarían: {changed_count} / {len(rows)}")
    print("=" * 50)

    if apply_changes:
        if updates_to_perform:
            cur.executemany(
                "UPDATE ads SET video_path = ?, image_path = ? WHERE id = ?",
                updates_to_perform,
            )
            conn.commit()
            print(f"✅ Se actualizaron exitosamente {len(updates_to_perform)} filas en la base de datos.")
        else:
            print("✨ Todas las rutas ya estaban en formato relativo.")
    else:
        print("💡 Para aplicar estas modificaciones permanentemente, ejecuta:")
        print("   python scripts/fix_paths.py --apply")

    conn.close()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Normalizar rutas absolutas a relativas en la DB de Jinora.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Aplica los cambios en la base de datos. Si no se pasa, corre en modo dry-run.",
    )
    args = parser.parse_args()
    sys.exit(run_fix_paths(apply_changes=args.apply))
