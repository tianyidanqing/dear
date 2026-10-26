"""自然语言解析：把用户的口语化描述拆成 标题 / 时间 / 时长 / 优先级。

支持中英双语（用户可能混说），例如：
  "明天下午3点开会，预计1小时，优先级高"
  "周五 上午10点 健身 45分钟 低"
  "meeting tomorrow 3pm for 1 hour, high priority"
  "gym Friday 10am 45 min low"

设计说明：
  - 这是**规则兜底**（默认关闭，见 `use_rule_fallback`）。主路径是大模型
    NLU，所以这里只覆盖常见表达，不求穷尽——规则是闭集，覆盖面越假越大，
    不如明确地只认「确定能处理」的句式。
  - 中英文规则**并存**，不按界面语言二选一：用户经常中英混用
    （「明天 review 一下 PR 30分钟」），按语言切换反而会漏掉。
  - 纯标准库实现，无第三方依赖。
"""
import re
from datetime import datetime, timedelta

from schedule_agent.domain.models import Priority
from schedule_agent.i18n import t

# 出现这些词/句式时，用户说的是「截止」，否则显式时刻一律视为「固定开始时间」
_DEADLINE_KEYWORDS = [
    "截止", "最晚", "不晚于", "最迟", "之前", "以前", "前完成", "前提交",
    "前交付", "前必须", "前搞定", "必须完成", "提交", "交付", "上报",
    "deadline", "ddl",
]
# 英文的截止表达。注意 "by " 可能误伤（"by the way"），但这里只用于兜底，
# 且真正的意图判断由大模型负责。
_EN_DEADLINE_KEYWORDS = (
    "deadline", "due by", "no later than", "not later than", "before ",
    "by ", "until ", "finish by",
)
_DEADLINE_TIME_RE = re.compile(r"\d{1,2}\s*(?:点|:)\s*\d{0,2}\s*(?:之)?前")

# 「不像在安排一件新任务」的信号：提问、纠正、复述、修改已有安排。
# 规则兜底（无大模型时）见到这些就不应该新增任务，否则会把闲聊建成垃圾待办。
_NON_TASK_PATTERNS = re.compile(
    r"^(为什么|为啥|怎么|怎样|什么|是不是|能否|可以吗|对吗|咋|有没有|哪天|哪个)"
    r"|(怎么办|什么意思|什么问题|怎么回事|不对|错了|重复了?|冲突)"
)
# 英文提问 / 纠正
_EN_NON_TASK_PATTERNS = re.compile(
    r"^(why|what|how|which|who|when|is|are|was|were|can|could|should|would|do|does|did|"
    r"am i|is it|are there)\b"
    r"|\b(wrong|incorrect|that's not|not right|duplicate|conflict|doesn't work)\b",
    re.I,
)
_CORRECTION_KEYWORDS = (
    "只要", "保留", "留下", "改成", "改为", "调成", "换成", "应该是",
    "应该是", "改成", "不要", "别", "重复",
)
_TIME_RE = re.compile(r"\d{1,2}\s*(?:点|:)|\b\d{1,2}\s*(?:a\.?m\.?|p\.?m\.?)\b", re.I)

# 「把已有安排挪到别的时间」的标记词。命中即可判定为 modify 意图，
# 而不是新增任务——否则「自习时间改成11点」会被存成一条叫「自习时间改成」的新待办。
_MODIFY_MARKERS = (
    "调整到", "调整成", "调整至", "推迟到", "推迟成", "提前到", "提前至",
    "延后到", "延后至", "推后到", "改到", "改成", "改为", "改在", "改至",
    "调到", "调成", "调至", "挪到", "挪成", "挪至", "换到", "换成", "换至",
    "推到", "推至",
)
# 英文语序是 `move <task> to <time>`，与中文的「把<task>改到<time>」方向不同，
# 所以单独用一条正则解析（见 parse_modify）。
_EN_MODIFY_RE = re.compile(
    r"\b(move|push|shift|reschedule|postpone|delay|change|adjust|bring)\b"
    r"\s+(?P<task>.+?)\s+(?:to|until|till|at)\s+(?P<time>.+)$",
    re.I,
)
# 从「标题关键词」里剥掉的前后缀（把X的开始时间改成… -> X）
_HINT_PREFIX = re.compile(r"^(?:请|麻烦|帮我|帮忙|辛苦|把|将|那个|这个|刚刚说的)+")
_HINT_SUFFIX = re.compile(r"(?:的)?(?:开始时间|开始时刻|的时间|时间|日程|安排)$")

# 英文虚词：清标题时剥掉（"meeting at 3pm for 1 hour" -> "meeting"）
_EN_STOPWORDS = re.compile(
    r"\b(?:at|on|in|for|by|from|to|the|a|an|please|about)\b", re.I)

_EN_WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2, "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}
_EN_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


def looks_like_non_task(text):
    """粗判一句话是不是「不像新增任务」（提问 / 纠正 / 讨论已有安排）。

    只用于**无大模型时的规则兜底**：命中则宁愿回一句引导，也不要凭空建任务。
    宁可少建，不可乱建——凭空多出来的垃圾待办会污染排期，比"没听懂"更糟。
    """
    t_ = (text or "").strip()
    if not t_:
        return True
    if _NON_TASK_PATTERNS.search(t_):
        return True
    if _EN_NON_TASK_PATTERNS.search(t_):
        return True
    if any(k in t_ for k in _CORRECTION_KEYWORDS):
        return True
    # 以语气词收尾的长句（"…只要早上9点的"）多半是对话而非任务标题
    if len(t_) >= 8 and re.search(r"[的了吗呢吧啊]$", t_) and not _TIME_RE.search(t_):
        return True
    # 英文：以问号结尾、或以 "?" 开头的疑问式长句
    if t_.endswith("?") and not _TIME_RE.search(t_):
        return True
    if len(t_) > 30:
        return True
    return False


def is_deadline(text):
    """判断文本里的时刻是「截止时间」还是「开始时间」。

    语义默认取反：**只要用户说出了明确时刻，默认是「这个点开始做」**（吃早茶、健身、
    开会都一样）；只有出现明确的截止语义（X 之前 / 截止 X / 最晚 X / 提交 / by X）
    才算 deadline。

    早期版本靠「会议/电话…」关键词猜 start，导致「今天10点吃早茶」这类日常安排被当成
    截止时间，排期时被塞进「当前时刻」的空隙（凌晨）。
    """
    if _DEADLINE_TIME_RE.search(text):
        return True
    low = text.lower()
    if any(k in low for k in _DEADLINE_KEYWORDS):
        return True
    return any(k in low for k in _EN_DEADLINE_KEYWORDS)


_WEEKDAYS = {
    "周一": 0, "星期一": 0, "周二": 1, "星期二": 1, "周三": 2, "星期三": 2,
    "周四": 3, "星期四": 3, "周五": 4, "星期五": 4, "周六": 5, "星期六": 5,
    "周日": 6, "星期日": 6, "周天": 6, "星期天": 6,
}


def _match_date_en(low, now):
    """英文日期：today / tomorrow / day after tomorrow / next Friday / Oct 20。

    返回 (date, 命中的原文片段) 或 (None, None)。
    """
    if re.search(r"\bday after tomorrow\b", low):
        return now.date() + timedelta(days=2), "day after tomorrow"
    if re.search(r"\btomorrow\b", low):
        return now.date() + timedelta(days=1), "tomorrow"
    if re.search(r"\btoday\b", low):
        return now.date(), "today"

    # 具体日期：Oct 20 / October 20th / 20 Oct
    m = re.search(r"\b([a-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b", low)
    if m and m.group(1) in _EN_MONTHS:
        try:
            return (now.date().replace(month=_EN_MONTHS[m.group(1)],
                                       day=int(m.group(2))), m.group(0))
        except ValueError:
            pass
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]{3,9})\b", low)
    if m and m.group(2) in _EN_MONTHS:
        try:
            return (now.date().replace(month=_EN_MONTHS[m.group(2)],
                                       day=int(m.group(1))), m.group(0))
        except ValueError:
            pass

    # 星期：next Friday / on Friday / fri
    m = re.search(r"\b(?:next\s+|on\s+|this\s+)?"
                  r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
                  r"mon|tues?|wed|thu(?:rs?)?|fri|sat|sun)\b", low)
    if m:
        wd = _EN_WEEKDAYS.get(m.group(1))
        if wd is not None:
            days_ahead = (wd - now.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7          # 说「周五」时如果今天就是周五，指下周五
            return now.date() + timedelta(days=days_ahead), m.group(0)
    return None, None


def _match_recurrence_en(low):
    """英文周期表达 -> 规则串；不命中返回 ""。"""
    if re.search(r"\b(daily|every\s*day|every\s+single\s+day)\b", low):
        return "daily"
    m = re.search(r"\bevery\s+(\d+)\s+days?\b", low)
    if m:
        return f"every:{int(m.group(1))}"
    if re.search(r"\b(monthly|every\s+month)\b", low):
        # 没给具体日号时，按今天取号
        return "monthly:" + str(datetime.now().day)
    if re.search(r"\b(weekly|every\s+week)\b", low):
        return "weekly:" + str(datetime.now().weekday())

    m = re.search(r"\b(?:every|on)\s+((?:(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*"
                  r"(?:\s*(?:,|and|&)\s*)?)+)", low)
    if m:
        days = sorted({_EN_WEEKDAYS[word] for word in
                       re.findall(r"(monday|tuesday|wednesday|thursday|friday|"
                                  r"saturday|sunday|mon|tues?|wed|thu(?:rs?)?|"
                                  r"fri|sat|sun)", m.group(1))})
        if days:
            return "weekly:" + ",".join(map(str, days))
    return ""


def parse_natural(text):
    """解析自然语言文本（中英双语），返回 dict：
    {title, due, start, duration_min, priority, recurrence}"""
    raw = text.strip()
    low = raw.lower()
    result = {
        "title": raw,
        "due": None,
        "start": None,
        "duration_min": 30,
        "priority": Priority.MEDIUM,
        "recurrence": "",
    }
    now = datetime.now()
    removed = []  # 记录需要从标题中剔除的片段

    # ---- 时长 ----
    for pat, mult in [
        (r"(\d+(?:\.\d+)?)\s*小时", 60), (r"(\d+)\s*分钟", 1),
        (r"(\d+(?:\.\d+)?)\s*(?:hours?|hrs?|h)\b", 60),
        (r"(\d+)\s*(?:minutes?|mins?|min)\b", 1),
    ]:
        m = re.search(pat, text, re.I)
        if m:
            result["duration_min"] = max(1, int(float(m.group(1)) * mult))
            removed.append(m.group(0))
            break

    # ---- 优先级（中文「高/中/低」| 英文 high/medium/low）----
    for kw, p in [("高", Priority.HIGH), ("中", Priority.MEDIUM), ("低", Priority.LOW)]:
        standalone = re.search(re.escape(kw) + r"\s*$", text)
        if re.search(r"优先级?" + kw, text) or re.search(kw + r"优先级", text) \
                or re.search(r"优先" + kw, text) or standalone:
            result["priority"] = p
            if standalone:
                removed.append(kw)
            break

    m = re.search(r"\b(high|medium|low)\b(?:\s+priority)?|\bpriority\s*[:=]?\s*(high|medium|low)\b",
                  low)
    if m:
        word = (m.group(1) or m.group(2) or "").lower()
        result["priority"] = {"high": Priority.HIGH, "medium": Priority.MEDIUM,
                              "low": Priority.LOW}.get(word, result["priority"])
        # 只有明显是「优先级说明」时才从标题里删，避免把 "high school" 删掉
        if "priority" in m.group(0) or re.search(r"\b(high|medium|low)\s*(,|$)", low):
            removed.append(m.group(0))

    # ---- 周期（每天 / 每周X / 每隔N天 / 每月D号 / daily / every Mon）----
    recurrence = ""
    m = re.search(r"每[天日]", text)
    if m:
        recurrence = "daily"
        removed.append(m.group(0))
    else:
        m = re.search(r"每(?:星期|周)([一二三四五六日天]+)", text)
        if m:
            wd_map = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
            days = sorted({wd_map[c] for c in m.group(1) if c in wd_map})
            recurrence = "weekly:" + ",".join(map(str, days))
            removed.append(m.group(0))
        else:
            m = re.search(r"每隔?(\d+)天", text)
            if m:
                recurrence = f"every:{int(m.group(1))}"
                removed.append(m.group(0))
            else:
                m = re.search(r"每月(\d{1,2})[号日]", text)
                if m:
                    recurrence = f"monthly:{int(m.group(1))}"
                    removed.append(m.group(0))
                else:
                    recurrence = _match_recurrence_en(low)
                    if recurrence:
                        # 命中片段不必精确定位，标题清理时再兜底
                        frag = re.search(r"\b(?:daily|every\s*\w+|weekly|monthly)\b", low)
                        if frag:
                            removed.append(frag.group(0))
    result["recurrence"] = recurrence

    # ---- 日期（周期任务无需指定具体日期）----
    date_part = now.date()
    if not recurrence:
        m = re.search(r"(\d{1,2})月(\d{1,2})[日号]?", text)
        if m:
            try:
                date_part = date_part.replace(month=int(m.group(1)), day=int(m.group(2)))
                removed.append(m.group(0))
            except ValueError:
                pass  # 非法日期（如 2月30日），忽略该匹配
        elif "后天" in text:
            date_part = now.date() + timedelta(days=2)
            removed.append("后天")
        elif "明天" in text or "明日" in text:
            date_part = now.date() + timedelta(days=1)
            removed.append("明天" if "明天" in text else "明日")
        elif "今天" in text or "今日" in text:
            date_part = now.date()
            removed.append("今天" if "今天" in text else "今日")
        else:
            hit = False
            for name, wd in _WEEKDAYS.items():
                if name in text:
                    days_ahead = (wd - now.weekday()) % 7
                    if days_ahead == 0:
                        days_ahead = 7
                    date_part = now.date() + timedelta(days=days_ahead)
                    removed.append(name)
                    hit = True
                    break
            if not hit:
                d, frag = _match_date_en(low, now)
                if d is not None:
                    date_part = d
                    removed.append(frag)

    # ---- 时间 ----
    hour = None
    minute = 0
    period = None

    m = re.search(r"(\d{1,2}):(\d{2})\s*(a\.?m\.?|p\.?m\.?)?", text, re.I)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        if m.group(3):
            period = "pm" if m.group(3).lower().startswith("p") else "am"
        removed.append(m.group(0).strip())
    if hour is None:
        m = re.search(r"\b(\d{1,2})\s*(a\.?m\.?|p\.?m\.?)(?![a-z])", text, re.I)
        if m:
            hour = int(m.group(1))
            period = "pm" if m.group(2).lower().startswith("p") else "am"
            removed.append(m.group(0).strip())
    if hour is None:
        m = re.search(r"(\d{1,2})点(?:(\d{1,2})分|三刻|一刻|半)?", text)
        if m:
            hour = int(m.group(1))
            if m.group(2):
                minute = int(m.group(2))
            elif m.group(0).endswith("半"):
                minute = 30
            elif m.group(0).endswith("三刻"):
                minute = 45
            elif m.group(0).endswith("一刻"):
                minute = 15
            removed.append(m.group(0))
    if hour is None:
        m = re.search(r"\bat\s+(\d{1,2})(?::(\d{2}))?\b", text, re.I)
        if m:
            hour = int(m.group(1))
            if m.group(2):
                minute = int(m.group(2))
            removed.append(m.group(0))
    if hour is None:
        m = re.search(r"\b(\d{1,2})\s*o'?clock\b", text, re.I)
        if m:
            hour = int(m.group(1))
            removed.append(m.group(0))

    if period is None:
        if any(k in text for k in ("下午", "晚上", "傍晚")) \
                or re.search(r"\b(p\.?m\.?|afternoon|evening|night)\b", low):
            period = "pm"
        elif any(k in text for k in ("上午", "早上", "早晨", "凌晨")) \
                or re.search(r"\b(a\.?m\.?|morning)\b", low):
            period = "am"

    if hour is not None:
        if period == "pm" and hour < 12:
            hour += 12
        if period == "am" and hour == 12:
            hour = 0
        dt = datetime(date_part.year, date_part.month, date_part.day, hour, minute)
        # 默认视为「固定开始时间」；只有明确的截止语义才当作截止时间
        if is_deadline(text):
            result["due"] = dt
        else:
            result["start"] = dt
        # 去掉时段词与截止标记词
        for k in ("下午", "上午", "早上", "早晨", "晚上", "傍晚", "凌晨",
                  "之前", "以前", "截止"):
            if k in text:
                removed.append(k)

    # ---- 清理标题 ----
    title = raw
    for frag in removed:
        if not frag:
            continue
        if re.search(r"[\u4e00-\u9fa5]", frag):
            title = title.replace(frag, " ")
        else:
            # 英文片段用词边界 + 忽略大小写，避免把 "exam" 里的 "am" 删掉
            title = re.sub(r"\b" + re.escape(frag) + r"\b", " ", title, flags=re.I)
    title = re.sub(r"\s+", " ", title).strip(" ，,。.、;；")
    # 「18点前提交」删掉时间片段后会剩一个孤立的「前」
    title = re.sub(r"^前\s*", "", title) if _DEADLINE_TIME_RE.search(text) else title
    # 英文残留虚词（"meeting at for" -> "meeting"）
    cleaned = _EN_STOPWORDS.sub(" ", title)
    if cleaned.strip():
        title = cleaned
    title = re.sub(r"\s+", " ", title).strip(" ，,。.、;；")
    result["title"] = title.strip() or t("nlp.default_title")
    return result


# 「改优先级」的标记词。只改优先级、不动时间——早期版本没有这个意图，
# 用户说「ai开发设置为高优先级」会被当成 add 新任务，凭空多出一条待办
# （且无时间，被排到「当前时刻」，凌晨排期就是这么来的）。
_PRIO_MODIFY_VERBS = (
    "设置", "设为", "设定", "改成", "改为", "调成", "调到", "调整为", "调整到",
    "提高", "降低", "提升", "降为", "升为", "变成", "置为",
    "调高", "调低", "升高", "提级", "降级",
)
# 优先级词 -> 枚举值（先长后短，避免「高优先级」被「优先高」半途截断）
_PRIO_PATTERNS = (
    (re.compile(r"优先级\s*(?:调整|设置|设|改|调|升|降|定)?\s*(?:为|成|到|至)?\s*(高|中|低)"), None),
    (re.compile(r"(高|中|低)\s*优先级"), None),
    (re.compile(r"优先\s*(高|中|低)"), None),
)
_PRIO_MAP = {"高": Priority.HIGH, "中": Priority.MEDIUM, "低": Priority.LOW}

# 英文：set ai-dev to high priority / ai-dev high priority / make X low
_EN_PRIO_PATTERNS = (
    re.compile(r"\bpriority\s*[:=]?\s*(high|medium|low)\b", re.I),
    re.compile(r"\b(high|medium|low)\s+priority\b", re.I),
    re.compile(r"\b(?:set|make|mark|bump|change|raise|lower|keep)\b.*?"
               r"\b(high|medium|low)\b", re.I),
)
_EN_PRIO_MAP = {"high": Priority.HIGH, "medium": Priority.MEDIUM, "low": Priority.LOW}
_EN_PRIO_VERBS = re.compile(
    r"\b(?:set|make|mark|bump|change|raise|lower|keep|as|to|it|please|priority)\b", re.I)


def parse_priority(text):
    """解析「只改优先级、不改时间」的语句，例如：

      ai开发设置为高优先级 / 把学习ai开发设为低优先级 / 自习优先级调高
      set ai-dev to high priority / make standup low priority

    返回 {"title_hint", "priority"}；不命中返回 None。

    只有当句子里**没有新的时间信息**时才生效——「下午3点开会 优先级高」是在
    安排新任务（add），不能被误判成修改。规则兜底与大模型共用这个判定。
    """
    raw = (text or "").strip()
    low = raw.lower()
    is_en = ("priority" in low or re.search(r"\b(high|medium|low)\b", low)) \
        and not ("优先" in raw)
    if not raw or ("优先" not in raw and not is_en):
        return None
    # 带新时间 -> 是新增/改时间，交给 add / parse_modify 处理
    if _TIME_RE.search(raw) \
            or any(k in raw for k in ("今天", "明天", "后天", "下周", "上午", "下午",
                                      "晚上", "早上")) \
            or re.search(r"\b(today|tomorrow|morning|afternoon|evening|tonight|"
                         r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
                         low):
        return None

    if is_en:
        prio, span = None, None
        for pat in _EN_PRIO_PATTERNS:
            m = pat.search(raw)
            if m:
                prio = _EN_PRIO_MAP.get(m.group(1).lower())
                span = m.span()
                break
        if prio is None:
            return None
        hint = (raw[:span[0]] + " " + raw[span[1]:])
        hint = _EN_PRIO_VERBS.sub(" ", hint)
        hint = re.sub(r"\s+", " ", hint).strip(" ,.!?;:")
        if not hint:
            return None
        return {"title_hint": hint, "priority": prio}

    prio, span = None, None
    for pat, _ in _PRIO_PATTERNS:
        m = pat.search(raw)
        if m:
            prio = _PRIO_MAP.get(m.group(1))
            span = m.span()
            break
    if prio is None:
        return None

    has_verb = any(v in raw for v in _PRIO_MODIFY_VERBS)
    # 没有「设为/改成」这类动词时，要求是「X高优先级」这种紧凑说法，
    # 且不像整句闲聊（长句多半是复述排期，不是指令）。
    if not has_verb and len(raw) > 12:
        return None

    hint = raw[:span[0]] + " " + raw[span[1]:]
    if not has_verb:
        hint = hint.replace("的", " ")
    for v in _PRIO_MODIFY_VERBS:
        hint = hint.replace(v, " ")
    hint = re.sub(r"(?:为|成|到|至|把|将|请|帮我|麻烦|那个|这个|一下|吧|了)+", " ", hint)
    hint = re.sub(r"\s+", " ", hint).strip(" ，,。.、!！?？~～的地得")

    if not has_verb and not hint:
        return None
    return {"title_hint": hint, "priority": prio}


def _clean_title_hint(left):
    """把「把自习的开始时间」这类左半句收敛成可用于匹配任务的标题关键词。"""
    h = (left or "").strip(" ，,。.、!！?？~～")
    for _ in range(3):
        h2 = _HINT_PREFIX.sub("", h)
        h2 = _HINT_SUFFIX.sub("", h2)
        h2 = h2.strip(" 的地得，,。.、")
        if h2 == h:
            break
        h = h2
    return h.strip()


def parse_modify(text):
    """解析「修改已有安排的时间」类语句，例如：

      自习时间改成11点 / 把吃早茶改到下午3点 / 晨会推迟到9点半
      move study to 11am / push standup to 9:30

    返回 {"title_hint", "start", "due", "duration_min"}，不是修改语句时返回 None。
    注意：这是**规则兜底**用的能力（无大模型时也能改时间），大模型可用时由 NLU 判定。
    """
    raw = (text or "").strip()
    if not raw:
        return None

    # ---- 英文语序：move <task> to <time> ----
    m = _EN_MODIFY_RE.search(raw)
    if m:
        task = _EN_STOPWORDS.sub(" ", m.group("task"))
        task = re.sub(r"\s+", " ", task).strip(" ,.!?;:")
        right = m.group("time")
        parsed = parse_natural(right)
        start, due = parsed.get("start"), parsed.get("due")
        if start is None and due is None:
            return None
        has_dur = bool(re.search(r"\d+\s*(?:hours?|hrs?|h|minutes?|mins?|min)\b",
                                 right, re.I))
        return {
            "title_hint": task,
            "start": start,
            "due": due,
            "duration_min": parsed.get("duration_min") if has_dur else None,
        }

    # ---- 中文语序：把<task>改到<time> ----
    pos, marker = -1, None
    for mk in _MODIFY_MARKERS:
        p = raw.find(mk)
        if p >= 0 and (pos < 0 or p < pos):
            pos, marker = p, mk
    if marker is None:
        return None

    left, right = raw[:pos], raw[pos + len(marker):].strip()
    if not right:
        return None

    parsed = parse_natural(right)
    start, due = parsed.get("start"), parsed.get("due")
    if start is None and due is None:
        return None  # 右半句里没有可识别的时间，交给上层追问

    has_duration = bool(re.search(
        r"(\d+(?:\.\d+)?)\s*(?:小时|分钟|hours?|hrs?|h\b|minutes?|mins?|min\b)",
        right, re.I))
    return {
        "title_hint": _clean_title_hint(left),
        "start": start,
        "due": due,
        "duration_min": parsed.get("duration_min") if has_duration else None,
    }


if __name__ == "__main__":
    for s in ["明天下午3点开会，预计1小时，优先级高",
              "周五 上午10点 健身 45分钟 低",
              "meeting tomorrow 3pm for 1 hour high priority",
              "gym Friday 10am 45 min low"]:
        print(s, "->", parse_natural(s))
