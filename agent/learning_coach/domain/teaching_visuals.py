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
    elif kind == "gallery":
        arrange = gallery_arrangement(spec)
        controls = ["点击图片放大"]
        if arrange == "timeline":
            controls += ["点时间点切换", "播放或暂停时间轴"]
        elif arrange == "tabs":
            controls += ["切换标签查看各格"]
        if spec["steps"]:
            controls += ["上一步", "下一步（分步讲解，会高亮对应的格）"]
        result.update(
            description="网络图片讲解页：" + "、".join(
                (panel["time"] + " " if panel["time"] else "") + panel["label"] for panel in spec["panels"]),
            controls=controls,
            observation_question=None,
            limitations=["图片来自网络（Wikimedia Commons 或网页搜索），由程序检索、审阅模型挑选，"
                         "只说明外观，不是原理证据；出处和许可证列在页面底部。"],
        )
        if spec["companion"] is not None:
            companion = visual_handoff(spec["companion"])
            result["companion_kind"] = companion["kind"]
            result["description"] += f"；页面下方附一张程序画的图（{companion['description']}）"
            result["controls"] += companion["controls"]
            result["observation_question"] = companion["observation_question"]
            result["limitations"] += companion["limitations"]
    return result


def gallery_arrangement(spec: dict) -> str:
    """The layout a gallery page actually uses; "auto" resolves from its content."""
    if spec["arrange"] != "auto":
        return spec["arrange"]
    panels = spec["panels"]
    if len(panels) >= 3 and all((panel["time"] or "").strip() for panel in panels):
        return "timeline"
    if len(panels) in (2, 3):
        return "side_by_side"
    return "tabs" if len(panels) >= 4 else "stack"
