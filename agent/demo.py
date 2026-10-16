"""Local interactive client for the same real five-agent pipeline; no deployment."""

import argparse
import asyncio
import json
from pathlib import Path
from uuid import uuid4

from learning_coach.server import create_pipeline


async def chat(args):
    pipeline = create_pipeline(data_dir=args.data_dir)
    session_id = args.session_id or str(uuid4())
    history, state = [], {}
    print(f"会话：{session_id}")
    print("输入 /memory on 开启笔记，/memory off 暂停，/forget 删除，/quit 退出。")
    try:
        while True:
            try:
                text = await asyncio.to_thread(input, "你：")
            except EOFError:
                break
            if text.strip() == "/quit":
                break
            if not text.strip():
                continue
            history.append({"role": "user", "text": text})
            response, state = await pipeline.run(history, session_id=session_id, state=state)
            history.append({"role": "assistant", "text": json.dumps(response, ensure_ascii=False)})
            print("教练：" + response["message"])
            if response["next_step"]:
                print(response["next_step"]["text"])
    finally:
        pipeline.store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--session-id", help="Reuse only a local session ID you own; this is not an authentication mechanism")
    asyncio.run(chat(parser.parse_args()))


if __name__ == "__main__":
    main()
