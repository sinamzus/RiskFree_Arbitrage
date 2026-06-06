#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
اجرای آنالیز TSETMC + ارسال خروجی برای Claude از طریق git push
=============================================================
این اسکریپت:
  ۱) tools/analyze_rizgheymat.py را اجرا می‌کند (روی نماد دلخواه)
  ۲) فایل خروجی tsetmc_analysis_output.txt را git add / commit می‌کند
  ۳) آن را به برنچ فعلی push می‌کند تا من بتوانم بخوانمش

اجرا (از ریشه‌ی پروژه):
    python tools/run_and_push_analysis.py
    python tools/run_and_push_analysis.py 3846143218462419
    python tools/run_and_push_analysis.py 3846143218462419 20260606

بعد از اجرا فقط بگو «پوش شد» — من خروجی را از مخزن می‌خوانم.
"""

import sys
import time
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ANALYZER = ROOT / "tools" / "analyze_rizgheymat.py"
VERIFIER = ROOT / "tools" / "verify_live.py"
OUTPUTS = [
    ROOT / "tsetmc_analysis_output.txt",
    ROOT / "verify_live_output.txt",
]


def run(cmd, **kw):
    print("».", " ".join(cmd))
    return subprocess.run(cmd, cwd=str(ROOT), **kw)


def main():
    args = sys.argv[1:]  # عبور دادن insCode / تاریخ به اسکریپت‌ها

    # ۱) اجرای آنالیزگر endpointها
    print("═" * 60)
    print("مرحله ۱: آنالیز endpointهای TSETMC ...")
    print("═" * 60)
    run([sys.executable, str(ANALYZER), *args])

    # ۲) اجرای تأیید مسیر کامل backend
    print("\n" + "═" * 60)
    print("مرحله ۲: تأیید مسیر دیتای زنده (کد واقعی backend) ...")
    print("═" * 60)
    run([sys.executable, str(VERIFIER), *args[:1]])  # فقط insCode

    present = [p for p in OUTPUTS if p.exists()]
    if not present:
        print("✗ هیچ فایل خروجی‌ای ساخته نشد.")
        sys.exit(1)
    for p in present:
        print(f"✅ خروجی: {p.name}  ({p.stat().st_size:,} بایت)")

    # برنچ فعلی
    br = run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
             capture_output=True, text=True)
    branch = br.stdout.strip() or "HEAD"
    print("برنچ فعلی:", branch)

    # add + commit
    print("\n" + "═" * 60)
    print("مرحله ۳: commit و push خروجی‌ها ...")
    print("═" * 60)
    run(["git", "add", "-f", *[str(p) for p in present]])

    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    msg = f"analysis: TSETMC ریز قیمت output {stamp}"
    c = run(["git", "commit", "-m", msg], capture_output=True, text=True)
    print(c.stdout.strip())
    if c.returncode != 0:
        # احتمالاً چیزی برای commit نبود (خروجی بدون تغییر)
        if "nothing to commit" in (c.stdout + c.stderr).lower():
            print("⚠ تغییری نسبت به قبل نبود — با --allow-empty دوباره تلاش می‌کنم.")
            run(["git", "commit", "--allow-empty", "-m", msg])
        else:
            print(c.stderr.strip())

    # ۴) push با چند بار تلاش (در صورت قطعی شبکه)
    delay = 2
    for attempt in range(1, 5):
        p = run(["git", "push", "-u", "origin", branch],
                capture_output=True, text=True)
        print(p.stdout.strip())
        if p.returncode == 0:
            print("\n✅ پوش شد. حالا فقط به Claude بگو «پوش شد».")
            return
        print(f"تلاش {attempt} ناموفق:", p.stderr.strip())
        if attempt < 4:
            print(f"  {delay} ثانیه صبر و تلاش مجدد ...")
            time.sleep(delay)
            delay *= 2

    print("\n✗ push ناموفق بود. لطفاً دستی اجرا کن:")
    print(f"    git push -u origin {branch}")


if __name__ == "__main__":
    main()
