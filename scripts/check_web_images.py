"""Try the web picture search once: Wikimedia Commons and (with BOCHA_API_KEY) BoCha.

Prints what each source returned for a few sample queries (title, source,
license, size, time) and saves the pictures to runs/web-images/ so they can
be looked at. Nothing else is sent anywhere.

  uv run --project agent --locked python scripts/check_web_images.py
"""

import asyncio
from pathlib import Path
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
SAMPLES = (("植物细胞 显微", "plant cell micrograph"), ("甲骨文", "oracle bone script"),
           ("竹简", "bamboo slips"))


async def main():
    from dotenv import load_dotenv
    load_dotenv(ROOT / "agent/.env")
    from learning_coach.adapters import image_search

    finder = image_search.default_image_finder()
    if finder is None:
        print("LEARNING_COACH_WEB_IMAGES=0：网络图片已关闭")
        return 1
    print("博查：" + ("已配置" if finder._bocha_api_key else "没有 BOCHA_API_KEY，只搜 Commons"))
    if finder._bocha_api_key:
        # Same endpoint the research step uses: tells a network problem from an image-specific one.
        from learning_coach.adapters.search_providers import BochaProvider
        started = perf_counter()
        try:
            pages = await BochaProvider(finder._bocha_api_key).search("植物细胞的结构")
            print(f"博查网页搜索（查资料用的那个）：正常，{len(pages)} 条，{perf_counter() - started:.1f} 秒")
        except Exception as error:
            reason = getattr(error, "reason", None)
            detail = f"：{str(reason)[:160]}" if isinstance(reason, (OSError, str)) else ""
            print(f"博查网页搜索（查资料用的那个）：也失败（{type(error).__name__}{detail}）")
    out = ROOT / "runs" / "web-images"
    out.mkdir(parents=True, exist_ok=True)
    for query_zh, query_en in SAMPLES:
        for label, function, args in (("Commons", image_search.commons_candidates, (query_en, 3)),
                                      ("博查", image_search.bocha_candidates, (finder._bocha_api_key, query_zh, 3))):
            if label == "博查" and not finder._bocha_api_key:
                continue
            started = perf_counter()
            try:
                listed = await asyncio.to_thread(function, *args)
                print(f"\n{label}「{query_zh if label == '博查' else query_en}」：{len(listed)} 个候选，"
                      f"{perf_counter() - started:.1f} 秒")
            except Exception as error:
                # A connection error's reason (DNS, TLS, refused) carries no key or request body;
                # other errors only show their type, since responses can carry request details.
                reason = getattr(error, "reason", None)
                detail = f"：{str(reason)[:160]}" if isinstance(reason, (OSError, str)) else ""
                print(f"\n{label}：失败（{type(error).__name__}{detail}）")
                continue
        started = perf_counter()
        found = await finder.find(query_zh, query_en, limit=3)
        print(f"→ 下载成功 {len(found)} 张，共 {perf_counter() - started:.1f} 秒")
        for number, item in enumerate(found, 1):
            suffix = item["media_type"].split("/")[1].replace("jpeg", "jpg")
            path = out / f"{query_en.replace(' ', '-')}-{number}.{suffix}"
            path.write_bytes(item["data"])
            print(f"  {number}. [{item['source']}] {item['title'][:60]}｜许可证 {item['license'] or '未知'}｜"
                  f"{len(item['data']) // 1024} KB → {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
