"""Check whether the deployed reviewer and designer models can read an image.

Sends one small generated picture (a red circle on the left, a blue square on
the right) to each distinct deployment used by the verifier, the visualizer
and the default model, through the same Foundry client the app uses, and
prints what each model saw. Two text-only calls go first, so the time a
picture adds and the one-off sign-in cost can be told apart. Also reports
output/reasoning tokens, optionally at a lower reasoning effort.

  uv run --project agent --locked python scripts/check_vision.py
  uv run --project agent --locked python scripts/check_vision.py --effort low

Needs `az login` and agent/.env with FOUNDRY_PROJECT_ENDPOINT.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
import struct
import sys
from time import perf_counter
import zlib

ROOT = Path(__file__).resolve().parents[1]
QUESTION = ("这张图左边和右边各画了什么颜色、什么形状？"
            '只返回 JSON：{"left": "<颜色和形状>", "right": "<颜色和形状>"}，不加围栏。')


def test_png(width=120, height=60) -> bytes:
    """White picture: red circle on the left half, blue square on the right half."""
    rows = []
    for y in range(height):
        row = bytearray([0])  # filter type 0
        for x in range(width):
            pixel = (255, 255, 255)
            if (x - 30) ** 2 + (y - 30) ** 2 <= 20 ** 2:
                pixel = (220, 30, 30)
            elif 72 <= x <= 108 and 12 <= y <= 48:
                pixel = (30, 60, 220)
            row += bytes(pixel)
        rows.append(bytes(row))

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b""))


def saw_it(text: str) -> bool:
    try:
        answer = json.loads(text.strip())
        left, right = str(answer.get("left", "")), str(answer.get("right", ""))
    except (ValueError, AttributeError):
        return False
    return "红" in left and "圆" in left and "蓝" in right and "方" in right


TEXT_QUESTION = '只返回 JSON：{"answer": <3 加 4 的结果>}，不加围栏。'


async def timed(agent, message, options):
    started = perf_counter()
    response = await asyncio.wait_for(agent.run(message, options=options), timeout=120)
    usage = getattr(response, "usage_details", None) or {}
    get = usage.get if isinstance(usage, dict) else (lambda key: getattr(usage, key, None))
    return response, (f"耗时 {perf_counter() - started:.1f} 秒，输出 token {get('output_token_count')}，"
                      f"其中推理 token {get('reasoning_output_token_count')}")


async def check(model, client, image, effort):
    from agent_framework import Agent, Content, Message

    agent = Agent(name="vision_check", client=client, instructions="你是看图核对员，只按要求返回 JSON。",
                  default_options={"store": False})
    options = {"reasoning": {"effort": effort}} if effort else {}
    print(f"{model}：")
    # Text first, so the one-off sign-in cost lands here and the two image
    # calls show the model's own time; text vs image shows what a picture adds.
    for label, message in (
            ("纯文字（第 1 次，含登录取凭证）", TEXT_QUESTION),
            ("纯文字（第 2 次）", TEXT_QUESTION),
            ("带图片", Message("user", [Content.from_text(QUESTION),
                                     Content.from_data(data=image, media_type="image/png")]))):
        try:
            response, numbers = await timed(agent, message, options)
        except Exception as error:  # report the type only; provider errors can carry request details
            print(f"  {label}：调用失败（{type(error).__name__}）")
            continue
        if label == "带图片":
            label += "，能看图" if saw_it(response.text) else "，回答不对，需要人工看一下"
        print(f"  {label}：{numbers}")
        print(f"    回答：{response.text.strip()[:120]}")


async def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--effort", choices=["none", "low", "medium", "high"], default="",
                        help="推理强度；不填用模型默认值")
    args = parser.parse_args()

    from dotenv import load_dotenv
    load_dotenv(ROOT / "agent/.env")
    from agent_framework_foundry import FoundryChatClient
    from azure.identity import DefaultAzureCredential

    default = os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"]
    models = {}
    for role in ("verifier", "visualizer"):
        models.setdefault(os.environ.get(f"LEARNING_COACH_MODEL_{role.upper()}", "").strip() or default, []).append(role)
    models.setdefault(default, []).append("默认")
    credential = DefaultAzureCredential()
    image = test_png()
    print(f"测试图：左边红色圆形，右边蓝色正方形（{len(image)} 字节）"
          + (f"，推理强度 {args.effort}" if args.effort else "，推理强度用默认值"))
    for model, roles in models.items():
        print(f"\n[{'、'.join(roles)}]")
        client = FoundryChatClient(project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"], model=model,
                                   credential=credential)
        await check(model, client, image, args.effort)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
