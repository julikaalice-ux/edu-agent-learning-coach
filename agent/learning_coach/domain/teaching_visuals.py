"""Bounded text-only teaching context derived from an accepted visual spec."""


def visual_handoff(spec: dict) -> dict:
    """Describe a validated visual for the teacher, without HTML/image bytes.

    Text from model-produced specs remains task data, never new instructions.
    Only supported controls are described; creating a picture does not verify
    its scientific correctness or imply that the learner has interacted with it.
    """
    kind = spec["kind"]
    result = {"kind": kind, "title": spec["title"], "description": "",
              "controls": [], "observation_question": None, "limitations": []}
    if kind == "scene":
        result.update(
            description=spec["learning_goal"] + "；步骤：" + " → ".join(frame["title"] for frame in spec["frames"]),
            controls=["选择步骤", "拖动步骤时间轴", "播放或暂停逐帧图解"],
            observation_question=spec["check_question"],
            interaction_hint=spec["interaction_hint"],
            limitations=[spec["limitations"], "这是交互分步图解，帧间切换不代表连续物理计算。"],
        )
    elif kind == "comparison":
        result.update(description=f"对照{spec['left_label']}与{spec['right_label']}；比较项：" +
                      "、".join(row["aspect"] for row in spec["rows"]),
                      controls=["选择一侧观察", "同时显示两侧对照"],
                      observation_question="切换两侧对照，先找出一个区别。")
    elif kind == "process":
        result.update(description="依次展示：" + " → ".join(step["title"] for step in spec["steps"]),
                      controls=["上一步", "下一步"],
                      observation_question="先看第一步，再用下一步检查它如何衔接。")
    elif kind == "flashcards":
        result.update(description=f"包含{len(spec['cards'])}张自测卡片。",
                      controls=["上一张", "翻卡查看答案", "下一张"],
                      observation_question="先回答第一张卡片，再翻面核对。")
    elif kind == "linear_function":
        result.update(description=f"观察一次函数 y=mx+b；初始 m={spec['slope']}，b={spec['intercept']}。",
                      controls=["调节斜率 m", "调节截距 b", "适应当前直线", "重置演示"],
                      observation_question="只调节一个参数，观察直线发生了什么变化？")
    elif kind == "diagram":
        result.update(description="关系示意图，包含：" + "、".join(node["label"] for node in spec["nodes"]))
    elif kind == "illustration":
        result.update(description=spec["alt"],
                      limitations=["AI 生成的静态配图，不是实测照片或物理模拟；细节可能不准确。"])
    return result
