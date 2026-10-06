"""Small, deterministic scene rules for the existing single reply-model call.

Adaptation details and complete upstream licenses: skills/communication/ORIGIN.md.
This module reads no files, resolves no identities and performs no network I/O.
"""
import re


VERSION = 'communication/1.0.1'
SCENES = ('auto', 'daily', 'work', 'customer', 'romance')

# A romantic topic in a received message is not consent to romantic assistance.
# Only the user's current goal or explicit scene can enable this scene. Clauses
# rejecting romantic wording do not count as an affirmative request.
_ROMANCE = re.compile(
    r'暧昧|调情|撩(?:妹|汉|人|她|他|一下|一撩|一会|一会儿)|泡妞|'
    r'恋爱|情侣|(?:我的|恋爱|追求)对象|男朋友|女朋友|喜欢的(?:人|女生|男生)|'
    r'(?:追求(?:她|他|对方|女生|男生)|想追(?:她|他|女生|男生)|表白|告白|约会)|情感沟通')
_NEGATED_ROMANCE = re.compile(
    r'(?:不要|不想|不用|不能|不必|别|避免|拒绝|停止|无需|不需要|不搞|不聊)'
    r'.{0,12}(?:暧昧|调情|撩|泡妞|恋爱|情侣|对象|约会|追求|表白|告白|情感沟通)')
_CUSTOMER_ROLE = re.compile(r'客户|甲方|顾客|买家|售后|客服|乙方|供应商')
_CUSTOMER_TASK = re.compile(r'报价|合同|订单|退款|退货|售后|投诉|交付|验收|付款|回款|开票|赔偿|折扣|货期|需求|延期')
_WORK_ROLE = re.compile(r'领导|老板|同事|主管|上级|下属|同部门|跨部门|项目组|职场|团队')
_WORK_TASK = re.compile(r'汇报|进度|排期|工期|加班|请假|任务|需求|项目|周报|审批|复盘|会议|工作|交接|上线|阻塞')
_WORK_STRONG = re.compile(r'工作汇报|项目进度|汇报进度|请假申请|跨部门协作|需求评审|排期调整|项目排期|工作交接')
_CUSTOMER_STRONG = re.compile(r'售后|退款申请|订单号|催回款|催付款|报价单|交付验收|退货申请')
_DAILY = re.compile(r'吃饭|吃了|晚饭|午饭|早饭|天气|下雨|周末|电影|游戏|散步|睡觉|晚安|早安|哈哈|好笑')
_AFFECTION = re.compile(r'(?:我(?:也|好|很|有点)?想你|我(?:也)?爱你|宝贝|亲爱的)')
_THIRD_PARTY = re.compile(r'他说|她说|别人|小说|电影|歌词|台词|转发|引用|不要|别叫|别说|不想|不是|没说')
_LABELS = {'daily': '日常', 'work': '职场', 'customer': '客户', 'romance': '情感恋爱'}


def scene_label(context, requested='auto'):
    """A local, bounded topic label; no identities, extra model call or old memory.

    Manual choice is authoritative. Romantic wording needs reciprocal direct
    messages, not a remark/name, quoted material or a third-party story.
    """
    if requested in _LABELS:
        return {'scene': requested, 'label': _LABELS[requested], 'origin': 'manual',
                'reason': '你为当前会话选择的风格'}
    rows = context.get('messages', []) if isinstance(context, dict) else []
    rows = rows[-12:] if isinstance(rows, list) else []
    direct = [(row, row['text'][:800]) for row in rows if isinstance(row, dict)
              and row.get('kind', 'text') == 'text' and isinstance(row.get('text'), str)
              and not row.get('quoted_content')]
    # Recent clear transaction requests outrank older personal exchanges.
    for row, value in reversed(direct):
        scene = _transaction_scene(value)
        if scene:
            return {'scene': scene, 'label': _LABELS[scene], 'origin': 'recent',
                    'reason': '根据最近 12 条消息中的明确话题判断'}
    affectionate = {row.get('is_self') for row, value in direct[-6:]
                    if type(row.get('is_self')) is bool and _AFFECTION.search(value)
                    and not _THIRD_PARTY.search(value)}
    if affectionate == {True, False}:
        return {'scene': 'romance', 'label': _LABELS['romance'], 'origin': 'recent',
                'reason': '根据近期双方直接表达的亲密话题判断，可随时调整'}
    if sum(bool(_DAILY.search(value)) for _, value in direct[-6:]) >= 2:
        return {'scene': 'daily', 'label': _LABELS['daily'], 'origin': 'recent',
                'reason': '根据近期生活闲聊话题判断'}
    return {'scene': None, 'label': '待确认', 'origin': 'uncertain',
            'reason': '近期信息不足，暂用自然日常语气；可以手动选择'}


def _goal_requests_romance(goal):
    for clause in re.split(r'[，,。；;\n！？!?]', goal):
        if _ROMANCE.search(clause) and not _NEGATED_ROMANCE.search(clause):
            return True
    return False


def _transaction_scene(text, *, explicit_goal=False):
    if (_CUSTOMER_ROLE.search(text) and
            (explicit_goal or _CUSTOMER_TASK.search(text))) or _CUSTOMER_STRONG.search(text):
        return 'customer'
    if (_WORK_ROLE.search(text) and
            (explicit_goal or _WORK_TASK.search(text))) or _WORK_STRONG.search(text):
        return 'work'
    return None


def select_scene(context, goal='', requested='auto'):
    """Return one resolved scene, with explicit selection taking precedence.

    Auto routing describes the current task, never a person's relationship or
    personality. Unknown/ambiguous contexts fall back to daily. Only the latest
    12 direct text messages can supply transaction hints; quoted cards, source
    names and metadata cannot authorize romantic mode.
    """
    if not isinstance(requested, str) or requested not in SCENES:
        raise ValueError('沟通场景无效，请选择自动、日常、职场、客户或情感。')
    if requested != 'auto':
        return requested
    if not isinstance(goal, str):
        raise ValueError('回复目标必须是文字。')
    goal = goal[:2000].strip()
    if _goal_requests_romance(goal):
        return 'romance'
    from_goal = _transaction_scene(goal, explicit_goal=True)
    if from_goal:
        return from_goal
    messages = context.get('messages', []) if isinstance(context, dict) else []
    if not isinstance(messages, list):
        return 'daily'
    # More recent messages take precedence: a formerly work-related conversation
    # must not become permanently classified as a work relationship.
    for message in reversed(messages[-12:]):
        if not isinstance(message, dict) or message.get('kind', 'text') != 'text':
            continue
        text = message.get('text')
        if isinstance(text, str):
            scene = _transaction_scene(text[:800])
            if scene:
                return scene
    inferred = scene_label(context).get('scene')
    if inferred == 'romance' and _NEGATED_ROMANCE.search(goal):
        return 'daily'
    return inferred or 'daily'


_COMMON = '''共同规则：本次明确要求优先，场景只调整表达方法，不证明联系人关系。先看双方最新往来及用户已经回答、取消或更正的内容，一轮回应一个主要需要。
个人口吻档案与样本只帮助选择句长、用词、标点、表情和幽默方式；不能据此补写本次事实、称呼、共同经历、关系或承诺。样本不足就自然直说，不扮演人设。
中文润色：先保留否定、条件、时间、归因与确定程度，再删空话、重复、无内容的客套和客服腔。保留必要礼貌及本人有意义的口头习惯，不机械禁词、凑段子、改错别字来假装本人，也不保证通过任何 AI 检测。
未知的信息可以省略；确实影响回应时，只澄清一个关键点，不强制先做问卷。严肃痛苦、道歉、冲突和正式事项中收住玩笑。策略说明只放分析，草稿保持可以直接编辑的自然中文；继续遵守现有 JSON 和证据 UID 合同。'''

_RULES = {
    'daily': '''日常聊天：接住对方这句话里一个具体细节，用本人适用的口吻回答或轻轻接梗。可以有小反差和画面感，不重复整句共情、不靠连续追问续聊。倾诉先回应实际处境，有需要再给建议。没有明确关系和用户目标，不主动加暧昧、亲密称呼或恋爱推进。''',
    'work': '''职场沟通：先回答实际问题或交代当前结果，再补最必要的进度、阻塞和需要对方确认的一点；短消息不套周报模板。领导、同事等身份只有原文或用户明确说明时才采用，不按备注猜上下级。
催进度时区分已完成、正在做和待确认；不给未知完成时间。新增任务或临时加需求时说明已知安排与取舍，必要时请求确认优先级。拒绝、异议和边界说清事情与影响，不讨好、不甩锅、不替用户承接责任。道歉只认有依据的具体责任；补救与期限必须是用户已明确愿意做的事。''',
    'customer': '''客户沟通：回应客户的具体问题与已知影响，说明当前能确认的处理、还需核实的范围，必要时给一个可执行的下一步；语气自然、平等，避免客服套话和内部术语。
报价、交付、退款、折扣、赔偿和后续联系时间只能沿用当前原文或用户明确提供的信息，不擅自许诺、不假称已退款或已上报。需求变化说清已确认范围和待确认部分。对方抱怨时先接住具体问题，认真处理，避免用幽默淡化损失。跟进未回复时简短提醒已有事项，不编紧迫性或反复催促。''',
    'romance': '''情感沟通：本次用户希望获得情感或约会回应帮助；这不代表双方已恋爱、互相喜欢或同意进一步亲密。只按已有话语与用户目标决定这一次的表达，不给人格、好感度、成功概率或关系阶段打分。
轻松接梗和有分寸的暧昧：围绕对方具体表达做一处轻巧回应或真诚赞赏，保留本人自然口吻；不硬造亲昵称呼，不编共同回忆、财力或生活经历，不默认对方性别或用贬低、吃醋试探、忽冷忽热制造压力。
邀约：有邀约目标时表达一个清楚、容易回答的提议；日期、地点和本人安排只用已知信息，未知时可以先问意愿。给对方自由选择，不把沉默当同意，也不替本人作长期承诺。
安慰：接具体处境和感受，少讲大道理，不把脆弱当推进关系的机会；道歉与关系修复只写具体行为、真实歉意和已授权的补救，不替对方猜心理、不追加无法保证的永远或一定。严肃场景收住玩笑。
安慰可以温和地回应眼前的处境，不必以陪伴或随时可用的保证收尾。除非用户明确愿意表达，不默认追加“随时找我”“我一直都在”“我都在”“我陪你”等持续待命或陪伴承诺；这不是禁用关心措辞，正常的关心、尊重对方暂不想说或不想复盘的意愿仍可直接表达。只说明当前能确认的态度，不替用户承诺后续时间与行动。
遇到明确拒绝、要求停止联系或取消邀约，尊重边界，改为平和确认或结束；不提供纠缠、施压、隐瞒或操控话术，不强行重启已结束的话题。''',
}


def prompt_for(scene):
    """Return fixed trusted rules; caller appends them to the existing system prompt."""
    if not isinstance(scene, str) or scene not in _RULES:
        raise ValueError('生成回复前需要确定有效的沟通场景。')
    return f'沟通技能 {VERSION} · {scene}\n{_COMMON}\n{_RULES[scene]}'
