"""云端素材包：角色模式同步 Windows person-material-sop-1；物品模式不变。
角色标准20=SOP固定编号；高级100=SOP20 + 扩展角度动作。
自定义提示词由 pack/server 处理，本文件只提供默认清单。"""
from recipes_item import item_recipes

# Windows SOP 20 张：category, framing, view, expression, action, pose
SOP20 = (
    ('head', '头肩', '正面平视', '平静闭嘴', '头摆正，双肩水平', 'head'),
    ('full_body', '完整全身', '正面平视', '平静闭嘴', '自然站立，双臂垂下，双脚平放', 'stand'),
    ('upper_body', '腰部以上半身', '正面平视', '闭嘴微笑', '双手在腰前轻搭', 'hands_front'),
    ('full_body', '完整全身', '向画面左前方转45度', '闭嘴微笑', '右手抬到肩膀以上挥手，左臂自然放松', 'wave'),
    ('head', '头肩', '向画面左转45度', '平静闭嘴', '头不仰不低，双肩放松', 'head'),
    ('full_body', '完整全身', '向画面右前方转45度', '严肃闭嘴', '迈步行走，一脚向前，双臂自然前后摆动', 'walk'),
    ('upper_body', '腰部以上半身', '向画面左前方转45度', '专注闭嘴', '右手在胸前摊掌说明，左手放松', 'explain'),
    ('full_body', '完整全身', '正面平视', '温和闭嘴微笑', '坐在普通椅子上，双手放膝上，双脚平放地面', 'sit'),
    ('head', '头肩', '向画面右转45度', '平静闭嘴', '头不仰不低，双肩放松', 'head'),
    ('full_body', '完整全身', '面向画面右方的90度侧面', '平静闭嘴', '侧面自然站立，双臂下垂', 'stand'),
    ('upper_body', '腰部以上半身', '向画面右前方转45度', '严肃闭嘴', '双臂在胸前轻松交叠', 'arms_crossed'),
    ('full_body', '完整全身', '面向画面左方的90度侧面', '平静闭嘴', '侧向迈步，双腿前后分开，双臂随步伐摆动', 'walk'),
    ('head', '完整侧面头肩', '面向画面左方的90度侧面', '平静闭嘴', '完整显示头顶、鼻尖、下巴和颈部', 'profile'),
    ('full_body', '完整全身', '正后方', '脸部不可见', '背对镜头站立，双臂自然放松', 'back'),
    ('upper_body', '腰部以上半身', '正面平视', '自然说话', '嘴巴小幅张开，双手在胸前自然作交谈手势', 'explain'),
    ('full_body', '完整全身', '正面平视', '自信闭嘴微笑', '重心落在一侧，一脚自然前移，双手放在身后', 'weight_shift'),
    ('expression', '面部特写', '正面平视', '平静闭嘴', '眉毛放松，嘴角平直，面部占画面主体', 'face'),
    ('expression', '面部特写', '正面平视', '明显闭嘴微笑', '嘴角上扬，脸颊轻抬，双唇保持闭合', 'face'),
    ('expression', '面部特写', '正面平视', '严肃闭嘴', '眉头轻皱，嘴唇轻抿，目光坚定', 'face'),
    ('expression', '面部特写', '正面平视', '自然说话', '嘴巴小幅张开，可见少量上排牙齿', 'face'),
)

DEFAULT_OUTFIT = '与参考人物上衣相配的完整日常服装和鞋子'

FACE_ACTION = {
    17: '面部肌肉自然松弛，眉间放松，双眉舒展，眼睛自然睁开。双唇轻轻接触闭合，嘴角水平，平静中性表情。',
    18: '明显的开心闭嘴微笑，两边嘴角同时向上扬，脸颊轻抬，眼神柔和。嘴唇贴合，不露牙。',
    19: '严肃而专注，双眉向眉心轻轻收拢，眉间轻皱，嘴唇轻抿闭合，嘴角平直，坚定注视镜头。',
    20: '自然说话的一瞬间，嘴巴小幅张开，上下唇清楚分开，可看见少量上排牙齿，眉毛放松，目光自然。',
}

# 高级100：在 SOP20 后追加扩展镜头（同一提示词模板）
EXTRA80 = (
    ('head', '头肩', '正面仰头约25度', '平静闭嘴', '下巴抬高，双肩放松', 'head'),
    ('full_body', '完整全身', '正面平视', '平静闭嘴', '双臂向两侧水平伸直侧平举', 'stand'),
    ('full_body', '完整全身', '正面平视', '平静闭嘴', '双臂斜上举、双脚分开，手脚张开呈大字形', 'stand'),
    ('upper_body', '腰部以上半身', '正面平视', '闭嘴微笑', '右手举过头顶，左臂自然下垂', 'explain'),
    ('full_body', '完整全身', '向画面左前方转45度', '平静闭嘴', '自然站立，双臂下垂', 'stand'),
    ('full_body', '完整全身', '向画面右前方转45度', '闭嘴微笑', '自然站立，双臂下垂', 'stand'),
    ('head', '头肩', '向画面左转30度', '平静闭嘴', '头不仰不低', 'head'),
    ('head', '头肩', '向画面右转30度', '平静闭嘴', '头不仰不低', 'head'),
    ('full_body', '完整全身', '正面平视', '自然说话', '迈步行走，一脚向前，双臂前后摆动', 'walk'),
    ('full_body', '完整全身', '正后方', '脸部不可见', '背对镜头行走', 'back'),
    ('upper_body', '腰部以上半身', '向画面左前方转45度', '闭嘴微笑', '双手叉腰', 'arms_crossed'),
    ('upper_body', '腰部以上半身', '向画面右前方转45度', '平静闭嘴', '双手自然下垂', 'hands_front'),
    ('full_body', '完整全身', '面向画面右方的90度侧面', '平静闭嘴', '侧面行走', 'walk'),
    ('full_body', '完整全身', '面向画面左方的90度侧面', '平静闭嘴', '侧面自然站立，双臂侧平举', 'stand'),
    ('head', '完整侧面头肩', '面向画面右方的90度侧面', '平静闭嘴', '完整显示头顶、鼻尖、下巴和颈部', 'profile'),
    ('full_body', '完整全身', '正面平视', '自信闭嘴微笑', '坐在普通椅子上，双手放膝上', 'sit'),
    ('full_body', '完整全身', '向画面左前方转45度', '严肃闭嘴', '迈步行走', 'walk'),
    ('full_body', '完整全身', '向画面右前方转45度', '闭嘴微笑', '右手抬到肩膀以上挥手', 'wave'),
    ('expression', '面部特写', '正面平视', '露齿微笑', '自然露齿微笑，眼神柔和', 'face'),
    ('expression', '面部特写', '正面平视', '轻微惊讶', '眉毛微抬，嘴巴微张', 'face'),
)

# 【待批准】头部/特写镜头按轮次换朝向和表情，避免 EXTRA80 循环 4 遍完全重复
HEAD_CYCLE = (
    ("正面平视", "平静闭嘴", "头摆正"),
    ("向画面左转30度并微微低头约10度", "闭嘴浅笑，嘴角轻轻上扬", "头微低，视线看向镜头"),
    ("向画面右转30度并微微抬头约10度", "专注闭嘴，眼神坚定", "下巴微抬"),
    ("头部向画面左侧轻歪约10度", "放松自然，嘴唇轻闭", "头轻歪，双肩放松"),
)
FACE_CYCLE = (
    None,
    "开心露齿笑，眼睛弯起，眉毛放松舒展。",
    "惊讶，眼睛睁大，眉毛轻轻上抬，嘴巴张开成小圆形。",
    "生气，眼神凌厉，双唇紧闭，下巴微收。",
)


def _upper_outfit(outfit):
    lower = ('腰', '裤', '裙', '鞋', '靴', '袜', '腿', '脚')
    kept = []
    for clause in __import__('re').split(r'[，,；;。]', outfit or ''):
        clause = clause.strip()
        if not clause:
            continue
        if any(w in clause for w in lower):
            if kept:
                break
            continue
        kept.append(clause)
    return '，'.join(kept)[:120] or outfit


def _prompt(n, category, framing, view, expression, action, pose, outfit=DEFAULT_OUTFIT):
    """Windows SOP 提示词模板；CFG=1，约束全部写在正面文字里。"""
    framing_map = {
        'head': '完整头肩肖像',
        'expression': '面部近景特写，面部占据画面主体',
        'upper_body': '腰部以上半身照，画面下缘在腰部',
        'full_body': '从头顶到鞋底的完整全身照片，头顶与鞋底四周留有空间',
    }
    fr = framing_map.get(category, framing)
    prompt = (f"制作一张写实人物照片。构图：{fr}。朝向：{view}。"
              f"人物正在做的动作：{action}。面部表情：{expression}。"
              '画面只有这一个人物。除第一张灰底画布外，其余每一张参考图都是同一个人，只用来识别脸型、五官、发型和服装，不要照搬任何一张参考图的姿势、头部朝向和表情，'
              '姿态与表情严格执行本张要求。保持真实自然的皮肤与清晰的眼睛眉毛嘴唇。')
    if category in ('full_body', 'upper_body'):
        visible = outfit
        if category == 'upper_body':
            visible = _upper_outfit(outfit)
        prompt += f'服装固定为{visible}。可见手部皮肤素净自然，手指结构自然。'
    if category == 'full_body':
        prompt += '人物穿好完整的鞋子，双脚和鞋底均完整入画。'
    if '闭嘴' in expression:
        prompt += '双唇自然合拢，嘴唇之间完全闭合。'
    if '45度' in view:
        side = '左' if '左' in view else '右'
        orientation = (f'首先固定三分之四侧面视角：人物的头部、肩膀和胸口一起向画面{side}方转45度，'
                       f'鼻尖和双眼视线均朝向画面{side}前方。近侧脸颊完整可见，远侧脸颊明显较窄，'
                       '两侧脸颊呈不对称的侧脸透视。')
        if category == 'full_body':
            orientation += f'髋部和双脚也朝向画面{side}前方。'
        prompt = orientation + prompt
    if '90度' in view:
        side = '左' if '左' in view else '右'
        prompt = (f'这是一张严格90度纯侧面照片，鼻尖朝画面{side}方。只看见近侧的一只眼睛，'
                  '近侧耳朵、鼻尖、嘴唇与下巴构成清楚的侧面轮廓，远侧眼睛被鼻梁遮挡。'
                  f'头、肩膀、胸口和髋部都正对画面{side}方，近侧肩膀遮住远侧肩膀。' + prompt)
    if pose == 'back' or view == '正后方':
        prompt = ('摄像机位于人物正后方。画面只展示完整后脑勺、双耳后缘、后颈、后背、裤腿后侧与鞋跟，'
                  '头部和身体一起背对镜头，自然直立，双臂垂在身体两侧。'
                  f'完整全身，头顶到鞋底全部入画。人物穿着{outfit}。'
                  '参考用于保持同一人物的头型、发型、年龄和体型。背面摄影，写实自然。')
    if category == 'expression' and n in FACE_ACTION:
        prompt = ('专业摄影棚人像，纯浅灰背景，柔和均匀的摄影灯光。摄像机正对面部，正面平视。'
                  '脸部近景特写，完整的头顶、双耳和下巴全部入画，完整面部无遮挡，画面下边缘在颈部与肩膀。'
                  + FACE_ACTION[n] + '画面里只出现这个人的头部、颈部及肩膀和空白灰背景，人物面对相机。'
                  '除第一张灰底画布外，其余每一张参考图都是同一个人，只用来识别脸型、五官、发型和服装，不要照搬任何一张参考图的姿势、头部朝向和表情。保留真实自然的皮肤质感、发丝和清楚的眼睛嘴唇。')
    if n == 7:
        visible = _upper_outfit(outfit)
        prompt = ('近距离腰部以上半身人像，画面底边切在腰带处，头和上半身充满画面。'
                  '人物右肘弯曲，右前臂向画面左侧伸出；右手在画面左半边，手掌水平朝天，像托着一个盘子，'
                  '五指舒展，拇指与四指分开。人物左臂在画面右侧自然下垂。'
                  '头部、肩膀和胸口一起朝画面左前方转45度，鼻尖向画面左方，闭嘴专注，嘴角平直。'
                  f'近景只显示头部、胸部、腰部和完整说明手势。服装固定为{visible}。'
                  '写实摄影，灰色纯色背景。除第一张灰底画布外，其余每一张参考图都是同一个人，只用来识别脸型、五官、发型和服装，不要照搬任何一张参考图的姿势、头部朝向和表情。'
                  '自然皮肤，眼睛、眉毛、嘴唇清晰。')
    return prompt


def _rows(shots):
    out = []
    for i, (category, framing, view, expression, action, pose) in enumerate(shots, 1):
        label = f'{i:02d}·{framing}·{action[:12]}'
        out.append({
            'id': f'standard_{i:03d}' if i <= 20 else f'advanced_{i:03d}',
            'label': f'{framing}·{action}' if len(action) < 18 else f'{framing}·{action[:16]}…',
            'category': category,
            'prompt': _prompt(i, category, framing, view, expression, action, pose),
        })
    return out


def _extra_rows(start_n=21):
    """把 EXTRA 循环扩展到凑满高级 100。"""
    rows = []
    n = start_n
    while len(rows) < 80:
        for category, framing, view, expression, action, pose in EXTRA80:
            if len(rows) >= 80:
                break
            c = (n - 21) // len(EXTRA80)
            if c and category == "head" and pose == "head":
                view, expression, action = HEAD_CYCLE[c % len(HEAD_CYCLE)]
            if c and category == "expression" and FACE_CYCLE[c % len(FACE_CYCLE)]:
                expression = action = FACE_CYCLE[c % len(FACE_CYCLE)]
            rows.append({
                'id': f'advanced_{n:03d}',
                'label': f'{framing}·{action}' if len(action) < 18 else f'{framing}·{action[:16]}…',
                'category': category,
                'prompt': _prompt(n, category, framing, view, expression, action, pose),
            })
            n += 1
    return rows


def character_recipes(mode):
    base = _rows(SOP20)
    if mode == 'standard':
        return base
    if mode == 'advanced':
        return base + _extra_rows(21)
    raise ValueError('素材包模式必须是 standard 或 advanced')


def base_recipes(mode, kind='character'):
    if kind == 'item':
        return item_recipes(mode)
    if kind != 'character':
        raise ValueError('模式必须是 character 或 item')
    return character_recipes(mode)


def recipes(mode, kind='character'):
    """默认清单 + 「固定提示词」覆盖（逐条替换，空白条目保留默认）。"""
    rows = base_recipes(mode, kind)
    import overrides
    ov = overrides.recipe_prompts(kind, mode)
    if ov:
        rows = [dict(r, prompt=ov[i]) if i < len(ov) and isinstance(ov[i], str) and ov[i].strip() else r for i, r in enumerate(rows)]
    return rows
