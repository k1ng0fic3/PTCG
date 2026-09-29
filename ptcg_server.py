"""
Single-file PTCG minimal web platform (Flask + Socket.IO)
- Run: python ptcg_server.py  (cardA.txt / cardB.txt are looked up next to this file)
- Serves GUI on port 9088
- Expects cardA.txt and cardB.txt (60 cards separated by spaces or newlines)

Dependencies:
  pip install flask flask-socketio simple-websocket

Features implemented:
- Two seats: A and B (players choose seat via UI; seats auto-release on disconnect)
- Player A can start the game which shuffles each player's deck (read from cardA.txt/cardB.txt)
- Draw button ("摸牌") for each player, remaining deck count shown
- Shuffle button ("洗牌") for each player to reshuffle that player's deck
- "奇树" button: shuffles both players' hands and places them under their deck, leaving deck order otherwise unchanged
- Zones: field (对战区), 5 bench slots (备战区), stadium (场地), hand (手牌, hidden to opponent), discard pile (弃牌)
- Same-named cards are auto-marked ①②③… when the game starts, so every card is unique
- Active/bench slots hold stacks: drop a card ON a card there to attach it (energy/tool/evolution);
  drop on an occupied slot's background to swap base cards (retreat, attachments stay in place)
- Drag and drop cards between visible zones:
  - each bench slot is its own drop target; dropping on the bench container uses the first empty slot
  - the server validates every move first, so a failed move never loses cards
- Deck search: double-click your deck to view it privately; right-click a card in the viewer to either
  "show it to the opponent and add to hand" or "add to hand secretly" (the log never leaks secret picks)
- Chat log on right (simple chat)
- Server keeps canonical game state and broadcasts updates (private hands are pushed only to their owner)

Notes:
- This is a minimal educational demo, not a full rules engine.
- Cards are represented as simple text strings (same-named cards are interchangeable).
"""
from flask import Flask, render_template_string, request
from flask_socketio import SocketIO, emit
import random
import os
import re

app = Flask(__name__)
app.config['SECRET_KEY'] = 'ptcg-secret'
# async_mode='threading': no eventlet needed; simple-websocket provides the
# WebSocket transport under threading mode
socketio = SocketIO(app, cors_allowed_origins='*', async_mode='threading')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Read cardlists
def load_cardlist(filename):
    path = os.path.join(BASE_DIR, filename)
    if not os.path.exists(path):
        return []
    with open(path, 'r', encoding='utf-8') as f:
        data = f.read().strip()
    if not data:
        return []
    # split by whitespace
    return data.split()

def circled(n):
    # ①..⑳ ㉑..㉟ ㊱..㊿, fallback (n)
    if 1 <= n <= 20:
        return chr(0x2460 + n - 1)
    if 21 <= n <= 35:
        return chr(0x2474 + n - 21)
    if 36 <= n <= 50:
        return chr(0x3251 + n - 36)
    return f'({n})'

def mark_duplicates(cards):
    # Prefix every same-named copy with ①②③… so each of the 60 cards is uniquely
    # identifiable (single-copy names stay clean). Cards are plain strings, so the
    # marker flows through every zone/move/log automatically.
    total = {}
    for c in cards:
        total[c] = total.get(c, 0) + 1
    counts = {}
    out = []
    for c in cards:
        if total[c] > 1:
            counts[c] = counts.get(c, 0) + 1
            out.append(f'{circled(counts[c])}{c}')
        else:
            out.append(c)
    return out

# Initial game state
state = {
    'players': {
        'A': None,  # will hold dict when player joins
        'B': None,
    },
    'decks': { 'A': [], 'B': [] },
    'hands': { 'A': [], 'B': [] },
    'benches': { 'A': [None]*5, 'B': [None]*5 },
    'active': { 'A': None, 'B': None },
    'stadium': { 'A': None, 'B': None },
    'discards': { 'A': [], 'B': [] },
    'log': [],
    'started': False,
}

# Helper: broadcast state to everyone and push each seated player's hand privately
def broadcast_state():
    masked = {
        'deck_counts': {k: len(v) for k, v in state['decks'].items()},  # contents are private (deck viewer)
        'hands_count': {k: len(v) for k, v in state['hands'].items()},
        'benches': state['benches'],
        'active': state['active'],
        'stadium': state['stadium'],
        'discards': state['discards'],
        'log': state['log'][-200:],
        'started': state['started'],
    }
    socketio.emit('state_update', masked)
    # push private hands to seated players (only they receive the contents)
    for seat in ('A', 'B'):
        sid = state['players'][seat]
        if sid:
            socketio.emit('private_hand', {'seat': seat, 'cards': list(state['hands'][seat])}, to=sid)

# Utility: push log
def push_log(text):
    state['log'].append(text)
    if len(state['log']) > 1000:
        state['log'] = state['log'][-1000:]

# Socket handlers
@socketio.on('connect')
def on_connect():
    emit('connected', {'msg': 'connected'})
    broadcast_state()

@socketio.on('disconnect')
def on_disconnect():
    # release any seat held by this connection so it doesn't stay occupied forever
    sid = request.sid
    for seat in ('A', 'B'):
        if state['players'][seat] == sid:
            state['players'][seat] = None
            push_log(f"玩家 {seat} 已断开连接，座位已释放。")
    broadcast_state()

@socketio.on('join')
def on_join(data):
    seat = data.get('seat')
    sid = request.sid
    if seat not in ('A', 'B'):
        emit('join_response', {'ok': False, 'error': 'seat must be A or B'})
        return
    if state['players'][seat] is not None and state['players'][seat] != sid:
        emit('join_response', {'ok': False, 'error': 'seat already taken'})
        return
    # release any other seat currently held by this connection (seat switching)
    for s in ('A', 'B'):
        if s != seat and state['players'][s] == sid:
            state['players'][s] = None
            push_log(f"玩家 {s} 已切换座位。")
    state['players'][seat] = sid
    push_log(f"玩家 {seat} 已连接。")
    emit('join_response', {'ok': True, 'seat': seat})
    broadcast_state()

@socketio.on('leave_seat')
def on_leave(data):
    seat = data.get('seat')
    if seat in ('A', 'B') and state['players'][seat] is not None:
        state['players'][seat] = None
        push_log(f"玩家 {seat} 已离开座位。")
    broadcast_state()

@socketio.on('start_game')
def on_start(data):
    seat = data.get('seat')
    # only seat A may start in this demo
    if seat != 'A':
        emit('action_resp', {'ok': False, 'error': '只有座位A可以开始'})
        return
    # load decks from files
    deckA = load_cardlist('cardA.txt')
    deckB = load_cardlist('cardB.txt')
    if not deckA or not deckB:
        missing = [f for f, d in (('cardA.txt', deckA), ('cardB.txt', deckB)) if not d]
        emit('action_resp', {'ok': False, 'error': f"卡组文件为空或不存在: {', '.join(missing)}"})
        return
    random.shuffle(deckA)
    random.shuffle(deckB)
    deckA = mark_duplicates(deckA)
    deckB = mark_duplicates(deckB)
    state['decks']['A'] = deckA
    state['decks']['B'] = deckB
    state['hands']['A'] = []
    state['hands']['B'] = []
    state['benches'] = {'A': [None]*5, 'B': [None]*5}
    state['active'] = {'A': None, 'B': None}
    state['stadium'] = {'A': None, 'B': None}
    state['discards'] = {'A': [], 'B': []}
    state['started'] = True
    push_log('游戏开始：牌堆已洗牌并准备就绪。')
    push_log('同名卡牌已用 ①②③ 编号以便区分；卡牌可直接拖到对战区/备战区的卡牌上进行附着（能量/道具/进化）。')
    broadcast_state()
    emit('action_resp', {'ok': True})

@socketio.on('draw')
def on_draw(data):
    seat = data.get('seat')
    if seat not in ('A', 'B'):
        emit('action_resp', {'ok': False, 'error': '无效座位'})
        return
    try:
        n = int(data.get('n', 1))
    except (TypeError, ValueError):
        n = 1
    n = max(1, min(n, 60))
    if not state['started']:
        emit('action_resp', {'ok': False, 'error': '游戏尚未开始'})
        return
    deck = state['decks'][seat]
    if not deck:
        emit('action_resp', {'ok': False, 'error': f'玩家 {seat} 的牌堆已空'})
        return
    hand = state['hands'][seat]
    drawn = []
    for i in range(n):
        if deck:
            card = deck.pop(0)
            hand.append(card)
            drawn.append(card)
    push_log(f"玩家 {seat} 摸牌 {len(drawn)} 张。")
    broadcast_state()
    emit('action_resp', {'ok': True, 'drawn': drawn})

@socketio.on('shuffle_deck')
def on_shuffle(data):
    seat = data.get('seat')
    if seat not in ('A', 'B'):
        emit('action_resp', {'ok': False, 'error': '无效座位'})
        return
    random.shuffle(state['decks'][seat])
    push_log(f"玩家 {seat} 洗牌。")
    broadcast_state()

@socketio.on('qishu')
def on_qishu(data):
    # 奇树：将双方手牌洗乱后放到各自牌堆底部，不改变其他牌堆顺序
    for s in ('A', 'B'):
        hand = state['hands'][s]
        if hand:
            random.shuffle(hand)
            state['decks'][s].extend(hand)
            state['hands'][s] = []
    push_log('奇树发动：双方手牌已洗乱并放入牌堆底部。')
    broadcast_state()

@socketio.on('move_card')
def on_move(data):
    # generic movement request from client:
    #   {seat, from_zone, to_zone, card, attach?}
    # from_zone: hand, deck, bench_0..bench_4, active, stadium, discard
    # to_zone:   hand, deck_top, deck_bottom, bench_0..bench_4, bench (first empty slot), active, stadium, discard
    # active/bench slots hold STACKS: [base pokemon, attached energy/tool/evolution, ...]
    #   - drop on an empty slot  -> card becomes the new base
    #   - drop on a card (attach=True) -> card is attached to that stack
    #   - drop on an occupied slot (no attach) -> swap base cards (retreat); attachments stay
    seat = data.get('seat')
    frm = data.get('from_zone')
    to = data.get('to_zone')
    card = data.get('card')
    attach = bool(data.get('attach'))

    if seat not in ('A', 'B'):
        emit('action_resp', {'ok': False, 'error': '无效座位'})
        return
    if not isinstance(card, str) or not card.strip():
        emit('action_resp', {'ok': False, 'error': '无效卡牌'})
        return

    def parse_zone(z, is_from):
        # returns (kind, bench_idx) or (None, None) if invalid
        if z in ('hand', 'active', 'stadium', 'discard'):
            return (z, None)
        if is_from and z == 'deck':
            return ('deck', None)
        if not is_from and z in ('deck_top', 'deck_bottom', 'bench'):
            return (z, None)
        m = re.fullmatch(r'bench_([0-4])', z) if isinstance(z, str) else None
        if m:
            return ('bench', int(m.group(1)))
        return (None, None)

    fzone, fidx = parse_zone(frm, True)
    tzone, tidx = parse_zone(to, False)
    if fzone is None or tzone is None:
        emit('action_resp', {'ok': False, 'error': f'无效区域: {frm} -> {to}'})
        return
    if frm == to and fidx == tidx:
        emit('action_resp', {'ok': False, 'error': '源区域与目标区域相同'})
        return

    def get_stack(zone, idx):
        if zone == 'active':
            return state['active'][seat]
        return state['benches'][seat][idx]

    def set_stack(zone, idx, val):
        if zone == 'active':
            state['active'][seat] = val
        else:
            state['benches'][seat][idx] = val

    # Resolve "bench" (no index) to the first empty slot before mutating anything
    if tzone == 'bench' and tidx is None:
        tidx = next((i for i in range(5) if state['benches'][seat][i] is None), None)
        if tidx is None:
            emit('action_resp', {'ok': False, 'error': '备战区已满'})
            return
        to = f'bench_{tidx}'

    # Locate the source stack (if any) and verify the card is really there
    src_stack = None
    if fzone in ('active', 'bench'):
        src_stack = get_stack(fzone, fidx)
        if not src_stack or card not in src_stack:
            emit('action_resp', {'ok': False, 'error': '无法从指定区域移除该卡'})
            return
    src_is_base = bool(src_stack) and src_stack[0] == card
    src_is_stadium = (fzone == 'stadium' and state['stadium'][seat] == card)

    # Is the destination an occupied slot/stadium we can swap base cards with?
    will_swap = False
    dest_base = None
    if tzone in ('active', 'bench'):
        tstack = get_stack(tzone, tidx)
        if tstack and not attach and (src_is_base or src_is_stadium):
            will_swap, dest_base = True, tstack[0]
    elif tzone == 'stadium':
        if state['stadium'][seat] is not None and (src_is_base or src_is_stadium):
            will_swap, dest_base = True, state['stadium'][seat]

    # Occupied destination without attach and without a swappable source -> reject
    # (checked BEFORE any mutation so a failed move never loses cards)
    if not will_swap:
        if tzone in ('active', 'bench') and get_stack(tzone, tidx) and not attach:
            emit('action_resp', {'ok': False, 'error': '目标位置已有卡牌；要附着能量/道具/进化，请将卡牌直接拖到那张卡上'})
            return
        if tzone == 'stadium' and state['stadium'][seat] is not None:
            emit('action_resp', {'ok': False, 'error': '场地已有卡牌，请先移走原场地卡'})
            return

    if will_swap:
        # exchange the two base/stadium cards; attachments stay with their slot
        if tzone == 'stadium':
            state['stadium'][seat] = card
        else:
            get_stack(tzone, tidx)[0] = card
        if src_is_base:
            src_stack[0] = dest_base
        else:
            state['stadium'][seat] = dest_base
        push_log(f"玩家 {seat} 交换了 {frm} 与 {to} 的卡牌（{card} ↔ {dest_base}），附着卡保持原位。")
        broadcast_state()
        emit('action_resp', {'ok': True})
        return

    # A base card with attachments cannot leave its slot until the attachments are moved away
    if fzone in ('active', 'bench') and src_is_base and len(src_stack) > 1:
        emit('action_resp', {'ok': False, 'error': '该宝可梦身上还有附着的卡牌，请先移走它们'})
        return

    # Remove card from source
    removed = False
    if fzone in ('hand', 'deck', 'discard'):
        lst = {'hand': state['hands'][seat], 'deck': state['decks'][seat], 'discard': state['discards'][seat]}[fzone]
        try:
            lst.remove(card)
            removed = True
        except ValueError:
            pass
    elif fzone == 'stadium':
        if src_is_stadium:
            state['stadium'][seat] = None
            removed = True
    else:  # slot zone (base alone, or an attachment)
        if src_is_base:
            set_stack(fzone, fidx, None)
        else:
            src_stack.remove(card)
        removed = True
    if not removed:
        emit('action_resp', {'ok': False, 'error': '无法从指定区域移除该卡'})
        return

    # Place card into destination
    if tzone == 'hand':
        state['hands'][seat].append(card)
        push_log(f"玩家 {seat} 将卡牌 {card} 移回手牌。")
    elif tzone == 'deck_top':
        state['decks'][seat].insert(0, card)
        push_log(f"玩家 {seat} 将卡牌 {card} 放到牌库顶。")
    elif tzone == 'deck_bottom':
        state['decks'][seat].append(card)
        push_log(f"玩家 {seat} 将卡牌 {card} 放到牌库底。")
    elif tzone == 'discard':
        state['discards'][seat].append(card)
        push_log(f"玩家 {seat} 将卡牌 {card} 弃掉。")
    elif tzone == 'stadium':
        state['stadium'][seat] = card
        push_log(f"玩家 {seat} 打出场地卡 {card}。")
    elif tzone in ('active', 'bench'):
        tstack = get_stack(tzone, tidx)
        if tstack:
            tstack.append(card)
            push_log(f"玩家 {seat} 将 {card} 附着到 {to} 的 {tstack[0]} 上。")
        else:
            set_stack(tzone, tidx, [card])
            push_log(f"玩家 {seat} 将 {card} 放到 {to}。")
    broadcast_state()
    emit('action_resp', {'ok': True})

@socketio.on('chat')
def on_chat(data):
    seat = data.get('seat') or '-'
    text = data.get('text')
    if not text:
        return
    push_log(f"[{seat}] {text}")
    broadcast_state()

@socketio.on('view_deck')
def on_view_deck(data):
    # open the private deck viewer: contents go ONLY to the seat owner
    seat = data.get('seat')
    sid = request.sid
    if seat not in ('A', 'B'):
        emit('action_resp', {'ok': False, 'error': '无效座位'})
        return
    if state['players'].get(seat) != sid:
        emit('action_resp', {'ok': False, 'error': '只能查看自己的牌库'})
        return
    if not state['started']:
        emit('action_resp', {'ok': False, 'error': '游戏尚未开始'})
        return
    push_log(f"玩家 {seat} 正在查看自己的牌库。")
    broadcast_state()
    socketio.emit('private_deck', {'seat': seat, 'cards': list(state['decks'][seat])}, to=sid)

@socketio.on('take_from_deck')
def on_take_from_deck(data):
    # search result: {seat, card, reveal}
    # reveal=True  -> card is shown to the opponent in the log
    # reveal=False -> log must NOT contain the card name (confidentiality)
    seat = data.get('seat')
    card = data.get('card')
    reveal = bool(data.get('reveal'))
    sid = request.sid
    if seat not in ('A', 'B') or not isinstance(card, str) or not card.strip():
        emit('action_resp', {'ok': False, 'error': '无效请求'})
        return
    if state['players'].get(seat) != sid:
        emit('action_resp', {'ok': False, 'error': '只能从自己的牌库拿牌'})
        return
    deck = state['decks'][seat]
    try:
        deck.remove(card)
    except ValueError:
        emit('action_resp', {'ok': False, 'error': '牌库中没有这张牌'})
        return
    state['hands'][seat].append(card)
    if reveal:
        push_log(f"玩家 {seat} 从牌库展示并加入手牌：{card}")
    else:
        push_log(f"玩家 {seat} 从牌库检索了一张牌加入手牌（内容保密）。")
    broadcast_state()
    # refresh the owner's deck viewer if it is open
    socketio.emit('private_deck', {'seat': seat, 'cards': list(state['decks'][seat])}, to=sid)
    emit('action_resp', {'ok': True})

# Serve the single-page UI
INDEX_HTML = '''
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Simple PTCG Demo</title>
  <style>
    body { font-family: Arial, sans-serif; display:flex; height:100vh; margin:0 }
    #left { flex:1; padding:10px; display:flex; flex-direction:column }
    #board { flex:1; display:flex; gap:10px }
    .player { flex:1; border:1px solid #ccc; padding:8px; display:flex; flex-direction:column }
    .zone { border:1px dashed #999; padding:6px; min-height:60px; margin-bottom:6px }
    .card { display:inline-block; padding:6px 8px; border-radius:6px; border:1px solid #666; margin:4px; cursor:grab; user-select:none; background:#fff }
    .bench-slot { display:inline-block; min-width:104px; min-height:56px; border:1px dashed #bbb; margin:2px; padding:2px; vertical-align:top }
    .card.att { display:block; font-size:12px; padding:2px 6px; background:#fff3c4; margin:2px 0 0 0 }
    .deck-zone { display:inline-block; min-width:150px; min-height:34px; border:1px dashed #4a7; border-radius:6px; padding:4px 8px; background:#f2fbf6; vertical-align:middle; cursor:pointer }
    .deck-zone.dragover { background:#d8f3e4; border-color:#2c8a5a }
    #deckModal { position:fixed; inset:0; background:rgba(0,0,0,.45); display:none; z-index:50 }
    #deckModal.open { display:flex; align-items:center; justify-content:center }
    #deckModalBox { background:#fff; border-radius:8px; padding:12px; width:90%; max-width:760px; max-height:80vh; display:flex; flex-direction:column }
    #deckModalCards { overflow:auto; display:flex; flex-wrap:wrap; gap:2px; padding:6px; border:1px dashed #999; margin-top:8px; align-content:flex-start }
    #ctxMenu { position:fixed; display:none; background:#fff; border:1px solid #888; border-radius:6px; box-shadow:0 2px 10px rgba(0,0,0,.3); z-index:100; padding:4px; min-width:190px }
    #ctxMenu button { display:block; width:100%; margin:2px 0; text-align:left }
    #controls { margin-bottom:8px }
    #right { width:360px; border-left:1px solid #ddd; padding:8px; box-sizing:border-box }
    #log { height:60vh; overflow:auto; background:#f7f7f7; padding:6px; border:1px solid #ddd }
    #chat { margin-top:8px }
    button { margin-right:6px }
    #msg { margin-left:12px; color:#b00 }
  </style>
</head>
<body>
  <div id="left">
    <div id="controls">
      Seat: <select id="seatSel"><option value="">-- choose --</option><option value="A">A</option><option value="B">B</option></select>
      <button id="joinBtn">坐下</button>
      <button id="leaveBtn">离开</button>
      <button id="startBtn">开始游戏 (仅A)</button>
      <span style="margin-left:12px">已选座位: <span id="mySeat">-</span></span>
      <span id="msg"></span>
    </div>
    <div id="board">
      <div class="player" id="playerA">
        <h3>玩家 A</h3>
        <div class="zone">对战区: <div id="activeA" class="zone"></div></div>
        <div class="zone">备战区: <div id="benchA" class="zone"></div></div>
        <div class="zone">场地: <div id="stadiumA" class="zone"></div></div>
        <div class="zone">弃牌: <div id="discardA" class="zone"></div></div>
        <div class="zone">手牌 (<span id="handCountA">0</span>): <div id="handA" class="zone"></div></div>
        <div style="margin-top:6px">
          <button id="drawA">摸牌</button>
          <button id="shuffleA">洗牌</button>
          <span class="deck-zone" id="deckA" title="双击=查看牌库；拖牌到此=放回牌库顶；Ctrl+拖入=牌库底">牌堆: <span id="deckCountA">0</span> 张</span>
        </div>
      </div>
      <div class="player" id="playerB">
        <h3>玩家 B</h3>
        <div class="zone">对战区: <div id="activeB" class="zone"></div></div>
        <div class="zone">备战区: <div id="benchB" class="zone"></div></div>
        <div class="zone">场地: <div id="stadiumB" class="zone"></div></div>
        <div class="zone">弃牌: <div id="discardB" class="zone"></div></div>
        <div class="zone">手牌 (<span id="handCountB">0</span>): <div id="handB" class="zone"></div></div>
        <div style="margin-top:6px">
          <button id="drawB">摸牌</button>
          <button id="shuffleB">洗牌</button>
          <span class="deck-zone" id="deckB" title="双击=查看牌库；拖牌到此=放回牌库顶；Ctrl+拖入=牌库底">牌堆: <span id="deckCountB">0</span> 张</span>
        </div>
      </div>
    </div>
    <div style="margin-top:8px">
      <button id="qishuBtn">奇树</button>
    </div>
  </div>
  <div id="right">
    <h3>对战日志 / 聊天</h3>
    <div id="log"></div>
    <div id="chat">
      <input id="chatInput" placeholder="请输入消息..." style="width:70%" />
      <button id="sendChat">发送</button>
    </div>
  </div>

  <div id="deckModal">
    <div id="deckModalBox">
      <div style="display:flex; align-items:center; gap:8px">
        <strong id="deckModalTitle">牌库</strong>
        <button id="deckModalShuffle">洗牌</button>
        <button id="deckModalClose" style="margin-left:auto">关闭</button>
      </div>
      <div style="font-size:12px; color:#666; margin-top:4px">第一张为牌库顶。右键点击卡牌：可选择「展示给对手并入手牌」或「直接入手牌（保密）」。检索完成后记得洗牌。</div>
      <div id="deckModalCards"></div>
    </div>
  </div>
  <div id="ctxMenu">
    <button id="ctxTakeReveal">展示给对手并入手牌</button>
    <button id="ctxTakeHide">直接入手牌（保密）</button>
  </div>

<script src="https://cdn.socket.io/4.6.1/socket.io.min.js"></script>
<script>
const socket = io();
let mySeat = null;

function $(id){return document.getElementById(id)}
function esc(s){ return String(s).replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function showMsg(t){
  const m = $('msg'); m.textContent = t;
  clearTimeout(showMsg._t);
  showMsg._t = setTimeout(()=>{ m.textContent=''; }, 3000);
}

socket.on('connected', d=>console.log(d));

socket.on('join_response', d=>{
  if(d.ok){
    mySeat = d.seat;                 // only treat ourselves as seated after the server confirms
    $('mySeat').textContent = d.seat;
  } else {
    alert(d.error || '加入失败');
  }
});

socket.on('action_resp', d=>{
  if(d && d.ok === false && d.error){ showMsg(d.error); }
});

function renderStack(el, seat, zone, stack){
  el.innerHTML = '';
  if(stack){
    stack.forEach((c,i)=>{
      const card = document.createElement('div');
      card.className = 'card' + (i>0 ? ' att' : '');
      card.textContent = c;
      card.dataset.seat = seat; card.dataset.zone = zone; card.draggable = true;
      el.appendChild(card);
    });
  } else {
    el.textContent = '[空]';
  }
}

function renderBench(elId, seat, stacks){
  const el = $(elId); el.innerHTML = '';
  stacks.forEach((stack,i)=>{
    const slot = document.createElement('div');
    slot.className = 'bench-slot'; slot.dataset.idx = i; slot.dataset.seat = seat;
    if(stack){
      stack.forEach((c,j)=>{
        const card = document.createElement('div');
        card.className = 'card' + (j>0 ? ' att' : '');
        card.textContent = c;
        card.dataset.seat = seat; card.dataset.zone = 'bench_' + i; card.draggable = true;
        slot.appendChild(card);
      });
    } else {
      slot.textContent = '[空]';
    }
    el.appendChild(slot);
  });
}

socket.on('state_update', s=>{
  renderBench('benchA','A',s.benches.A);
  renderBench('benchB','B',s.benches.B);

  renderStack($('activeA'),'A','active',s.active.A);
  renderStack($('activeB'),'B','active',s.active.B);
  const stadiumA = $('stadiumA'); stadiumA.innerHTML=''; if(s.stadium.A){const el=document.createElement('div'); el.className='card'; el.textContent=s.stadium.A; el.dataset.seat='A'; el.dataset.zone='stadium'; el.draggable=true; stadiumA.appendChild(el)} else stadiumA.textContent='[空]';
  const stadiumB = $('stadiumB'); stadiumB.innerHTML=''; if(s.stadium.B){const el=document.createElement('div'); el.className='card'; el.textContent=s.stadium.B; el.dataset.seat='B'; el.dataset.zone='stadium'; el.draggable=true; stadiumB.appendChild(el)} else stadiumB.textContent='[空]';

  const discardA = $('discardA'); discardA.innerHTML=''; s.discards.A.forEach(c=>{const el=document.createElement('div'); el.className='card'; el.textContent=c; el.dataset.seat='A'; el.dataset.zone='discard'; el.draggable=true; discardA.appendChild(el)});
  const discardB = $('discardB'); discardB.innerHTML=''; s.discards.B.forEach(c=>{const el=document.createElement('div'); el.className='card'; el.textContent=c; el.dataset.seat='B'; el.dataset.zone='discard'; el.draggable=true; discardB.appendChild(el)});

  // hand counts (opponent hand contents are never sent to us)
  $('handCountA').textContent = s.hands_count.A;
  $('handCountB').textContent = s.hands_count.B;
  // deck counts
  $('deckCountA').textContent = s.deck_counts.A;
  $('deckCountB').textContent = s.deck_counts.B;

  // log (escaped so chat text containing < > & cannot break rendering)
  const logEl = $('log'); logEl.innerHTML = s.log.map(l=>'<div>'+esc(l)+'</div>').join(''); logEl.scrollTop = logEl.scrollHeight;
});

// private hand contents (pushed by the server only to the owner)
socket.on('private_hand', d=>{
  if(d.seat == mySeat){
    const handEl = document.getElementById('hand'+d.seat);
    handEl.innerHTML='';
    d.cards.forEach(c=>{const el=document.createElement('div'); el.className='card'; el.textContent=c; el.draggable=true; el.dataset.zone='hand'; el.dataset.seat=d.seat; handEl.appendChild(el);});
  }
});

// Simple helpers for UI actions
$('joinBtn').onclick = ()=>{
  const seat = $('seatSel').value;
  if(!seat) return alert('请选择座位');
  socket.emit('join', {seat});  // mySeat is set in join_response after the server confirms
}
$('leaveBtn').onclick = ()=>{ if(!mySeat) return; socket.emit('leave_seat',{seat:mySeat}); mySeat=null; $('mySeat').textContent='-'; }
$('startBtn').onclick = ()=>{ if(!mySeat) return alert('请先坐下'); socket.emit('start_game',{seat:mySeat}); }
$('drawA').onclick = ()=>{ socket.emit('draw',{seat:'A', n:1}); }
$('drawB').onclick = ()=>{ socket.emit('draw',{seat:'B', n:1}); }
$('shuffleA').onclick = ()=>{ socket.emit('shuffle_deck',{seat:'A'}); }
$('shuffleB').onclick = ()=>{ socket.emit('shuffle_deck',{seat:'B'}); }
$('qishuBtn').onclick = ()=>{ socket.emit('qishu',{}); }
$('sendChat').onclick = ()=>{ const t = $('chatInput').value.trim(); if(!mySeat) return alert('请先坐下'); if(!t) return; socket.emit('chat',{seat:mySeat, text:t}); $('chatInput').value=''; }

// Drag & drop: use native dragstart to populate dataTransfer
document.addEventListener('dragstart', function(ev){
  const el = ev.target.closest ? ev.target.closest('.card') : null;
  if(!el) return;
  // don't allow dragging placeholders
  if(el.textContent === '[空]' || el.textContent === '[隐藏]'){
    ev.preventDefault();
    return;
  }
  const seat = el.dataset.seat || '';
  const zone = el.dataset.zone || '';
  const cardText = el.textContent.trim();
  try{
    ev.dataTransfer.setData('text/plain', JSON.stringify({seat: seat, card: cardText, zone: zone}));
    ev.dataTransfer.effectAllowed = 'move';
  }catch(e){
    console.warn('dragstart setData failed', e);
  }
});

// Accept drops on zone containers
function setupDrop(id, to_zone, seat){
  const el = $(id);
  el.addEventListener('dragover', function(ev){ ev.preventDefault(); });
  el.addEventListener('drop', function(ev){ ev.preventDefault();
    let d = ev.dataTransfer.getData('text/plain');
    try{ d = JSON.parse(d); } catch(e){ return; }
    if(!mySeat) return alert('请先坐下');
    // only allow moving your own cards, or public cards (opponent hand is never rendered anyway)
    if(d.seat !== mySeat && d.zone === 'hand'){
      return alert('不能移动对手手牌');
    }
    socket.emit('move_card', {seat: d.seat, from_zone: d.zone, to_zone: to_zone, card: d.card});
  });
}

// Slot zones (active/bench): dropping directly ON a card attaches to that stack
// (energy/tool/evolution); dropping on the slot background places a new base or
// swaps base cards; dropping on the bench container gap uses the first empty slot.
function setupSlotDrop(id, seat, kind){ // kind: 'active' | 'bench'
  const el = $(id);
  el.addEventListener('dragover', function(ev){ ev.preventDefault(); });
  el.addEventListener('drop', function(ev){ ev.preventDefault();
    let d = ev.dataTransfer.getData('text/plain');
    try{ d = JSON.parse(d); } catch(e){ return; }
    if(!mySeat) return alert('请先坐下');
    if(d.seat !== mySeat && d.zone === 'hand'){
      return alert('不能移动对手手牌');
    }
    const cardEl = ev.target.closest ? ev.target.closest('.card') : null;
    let to_zone, attach = false;
    if(kind === 'active'){
      to_zone = 'active'; attach = !!cardEl;
    } else {
      const slot = ev.target.closest ? ev.target.closest('.bench-slot') : null;
      if(slot){ to_zone = 'bench_' + slot.dataset.idx; attach = !!cardEl; }
      else to_zone = 'bench';
    }
    socket.emit('move_card', {seat: d.seat, from_zone: d.zone, to_zone: to_zone, card: d.card, attach: attach});
  });
}

// Deck: plain drop = top of deck, Ctrl+drop = bottom of deck
function setupDeckDrop(id, seat){
  const el = $(id);
  el.addEventListener('dragover', function(ev){ ev.preventDefault(); el.classList.add('dragover'); });
  el.addEventListener('dragleave', function(){ el.classList.remove('dragover'); });
  el.addEventListener('drop', function(ev){ ev.preventDefault(); el.classList.remove('dragover');
    let d = ev.dataTransfer.getData('text/plain');
    try{ d = JSON.parse(d); } catch(e){ return; }
    if(!mySeat) return alert('请先坐下');
    if(d.seat !== mySeat && d.zone === 'hand'){
      return alert('不能移动对手手牌');
    }
    const to_zone = ev.ctrlKey ? 'deck_bottom' : 'deck_top';
    socket.emit('move_card', {seat: d.seat, from_zone: d.zone, to_zone: to_zone, card: d.card});
  });
}

setupSlotDrop('activeA','A','active');
setupSlotDrop('activeB','B','active');
setupSlotDrop('benchA','A','bench');
setupSlotDrop('benchB','B','bench');
setupDrop('stadiumA','stadium','A');
setupDrop('stadiumB','stadium','B');
setupDrop('discardA','discard','A');
setupDrop('discardB','discard','B');
setupDrop('handA','hand','A');
setupDrop('handB','hand','B');
setupDeckDrop('deckA','A');
setupDeckDrop('deckB','B');

// ---- Deck viewer (double-click the deck) ----
let deckViewSeat = null;

function renderDeckView(cards){
  $('deckModalTitle').textContent = '我的牌库（' + cards.length + ' 张）';
  const c = $('deckModalCards'); c.innerHTML = '';
  cards.forEach(card=>{
    const el = document.createElement('div');
    el.className = 'card'; el.textContent = card;
    el.addEventListener('contextmenu', function(ev){
      ev.preventDefault();
      showCtxMenu(ev.clientX, ev.clientY, card);
    });
    c.appendChild(el);
  });
  $('deckModal').classList.add('open');
}

function closeDeckView(){
  $('deckModal').classList.remove('open');
  hideCtxMenu();
  deckViewSeat = null;
}

function showCtxMenu(x, y, card){
  const m = $('ctxMenu');
  m.dataset.card = card;
  m.style.display = 'block';
  // keep the menu inside the viewport
  const r = m.getBoundingClientRect();
  m.style.left = Math.min(x, window.innerWidth - r.width - 4) + 'px';
  m.style.top = Math.min(y, window.innerHeight - r.height - 4) + 'px';
}
function hideCtxMenu(){ $('ctxMenu').style.display = 'none'; }

// private deck contents (sent only to the owner)
socket.on('private_deck', d=>{
  if(d.seat === mySeat){
    deckViewSeat = d.seat;
    renderDeckView(d.cards);
  }
});

$('ctxTakeReveal').onclick = ()=>{
  const card = $('ctxMenu').dataset.card;
  if(card && deckViewSeat) socket.emit('take_from_deck', {seat: deckViewSeat, card: card, reveal: true});
  hideCtxMenu();
};
$('ctxTakeHide').onclick = ()=>{
  const card = $('ctxMenu').dataset.card;
  if(card && deckViewSeat) socket.emit('take_from_deck', {seat: deckViewSeat, card: card, reveal: false});
  hideCtxMenu();
};

$('deckModalClose').onclick = closeDeckView;
$('deckModal').addEventListener('click', ev=>{ if(ev.target === $('deckModal')) closeDeckView(); });
document.addEventListener('keydown', ev=>{ if(ev.key === 'Escape') closeDeckView(); });
document.addEventListener('click', ev=>{ if(!ev.target.closest('#ctxMenu')) hideCtxMenu(); });

// shuffle from within the viewer, then refresh the view
$('deckModalShuffle').onclick = ()=>{
  if(!deckViewSeat) return;
  socket.emit('shuffle_deck', {seat: deckViewSeat});
  socket.emit('view_deck', {seat: deckViewSeat});
};

function setupDeckView(id, seat){
  $(id).addEventListener('dblclick', ()=>{
    if(!mySeat) return alert('请先坐下');
    socket.emit('view_deck', {seat: seat});
  });
}
setupDeckView('deckA','A');
setupDeckView('deckB','B');

</script>
</body>
</html>
'''

@app.route('/')
def index():
    return render_template_string(INDEX_HTML)

# Fallback: a client may explicitly ask for its own hand (owner-verified)
@socketio.on('request_hand')
def on_request_hand(data):
    seat = data.get('seat')
    sid = request.sid
    if seat in ('A', 'B') and state['players'].get(seat) == sid:
        socketio.emit('private_hand', {'seat': seat, 'cards': list(state['hands'][seat])}, to=sid)

if __name__ == '__main__':
    print('Starting PTCG demo server on port 9088...')
    socketio.run(app, host='0.0.0.0', port=9088)
