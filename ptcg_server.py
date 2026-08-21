"""
Single-file PTCG minimal web platform (Flask + Socket.IO)
- Run on Linux: python3 ptcg_server.py
- Serves GUI on port 9088
- Expects cardA.txt and cardB.txt in same directory (60 cards separated by spaces or newlines)

Dependencies:
  pip install flask flask-socketio eventlet

Features implemented:
- Two seats: A and B (players choose seat via UI)
- Player A can start the game which shuffles each player's deck (read from cardA.txt/cardB.txt)
- Draw button ("摸牌") for each player
- Shuffle button ("洗牌") for each player to reshuffle that player's deck
- "奇树" button: shuffles both players' hands and places them under their deck, leaving deck order otherwise unchanged
- Zones: field (对战区), 5 bench slots (备战区), stadium (场地), hand (手牌, hidden to opponent), discard pile (弃牌)
- Drag and drop cards between visible zones; hand is hidden to opponent (only counts shown)
- Chat log on right (simple chat)
- Server keeps canonical game state and broadcasts updates

Notes:
- This is a minimal educational demo, not a full rules engine.
- Cards are represented as simple text strings.

"""
from flask import Flask, render_template_string, send_from_directory, request
from flask_socketio import SocketIO, emit, join_room, leave_room
import random
import os
import eventlet
eventlet.monkey_patch()

app = Flask(__name__)
app.config['SECRET_KEY'] = 'ptcg-secret'
socketio = SocketIO(app, cors_allowed_origins='*')

# Read cardlists
def load_cardlist(filename):
    if not os.path.exists(filename):
        return []
    with open(filename, 'r', encoding='utf-8') as f:
        data = f.read().strip()
    if not data:
        return []
    # split by whitespace
    cards = data.split()
    return cards

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

# Helper: broadcast state to everyone (but hide opponent hands on client side OR send counts only)
def broadcast_state():
    # For privacy, we will send full state but clients should hide opponent hands. For safety also send hand counts.
    masked = dict(state)
    # create a shallow copy for decks/hands to avoid accidental modification
    masked = {
        'decks': {k: list(v) for k,v in state['decks'].items()},
        'hands_count': {k: len(v) for k,v in state['hands'].items()},
        'benches': state['benches'],
        'active': state['active'],
        'stadium': state['stadium'],
        'discards': state['discards'],
        'log': state['log'][-200:],
        'started': state['started'],
    }
    socketio.emit('state_update', masked)

# Utility: push log
def push_log(text):
    state['log'].append(text)
    if len(state['log'])>1000:
        state['log']=state['log'][-1000:]

# Socket handlers
@socketio.on('connect')
def on_connect():
    emit('connected', {'msg':'connected'})
    broadcast_state()

@socketio.on('join')
def on_join(data):
    seat = data.get('seat')
    sid = request.sid
    if seat not in ('A','B'):
        emit('join_response', {'ok':False, 'error':'seat must be A or B'})
        return
    if state['players'][seat] is not None and state['players'][seat] != sid:
        emit('join_response', {'ok':False, 'error':'seat already taken'})
        return
    state['players'][seat] = sid
    push_log(f"玩家 {seat} 已连接。")
    emit('join_response', {'ok':True, 'seat':seat})
    broadcast_state()

@socketio.on('leave_seat')
def on_leave(data):
    seat = data.get('seat')
    if seat in ('A','B'):
        state['players'][seat] = None
        push_log(f"玩家 {seat} 已离开座位。")
    broadcast_state()

@socketio.on('start_game')
def on_start(data):
    seat = data.get('seat')
    # only seat A may start in this demo
    if seat != 'A':
        emit('action_resp', {'ok':False, 'error':'只有座位A可以开始'})
        return
    # load decks from files
    deckA = load_cardlist('cardA.txt')
    deckB = load_cardlist('cardB.txt')
    if len(deckA) < 1 or len(deckB) < 1:
        emit('action_resp', {'ok':False, 'error':'请确保 cardA.txt 和 cardB.txt 存在并包含卡牌'})
        return
    random.shuffle(deckA)
    random.shuffle(deckB)
    state['decks']['A'] = deckA
    state['decks']['B'] = deckB
    state['hands']['A'] = []
    state['hands']['B'] = []
    state['benches'] = {'A':[None]*5,'B':[None]*5}
    state['active'] = {'A':None,'B':None}
    state['stadium'] = {'A':None,'B':None}
    state['discards'] = {'A':[],'B':[]}
    state['started'] = True
    push_log('游戏开始：牌堆已洗牌并准备就绪。')
    broadcast_state()
    emit('action_resp', {'ok':True})

@socketio.on('draw')
def on_draw(data):
    seat = data.get('seat')
    n = int(data.get('n',1))
    if not state['started']:
        emit('action_resp', {'ok':False, 'error':'游戏尚未开始'})
        return
    deck = state['decks'][seat]
    hand = state['hands'][seat]
    drawn = []
    for i in range(n):
        if deck:
            card = deck.pop(0)
            hand.append(card)
            drawn.append(card)
    push_log(f"玩家 {seat} 摸牌 {len(drawn)} 张。")
    broadcast_state()
    emit('action_resp', {'ok':True, 'drawn':drawn})

@socketio.on('shuffle_deck')
def on_shuffle(data):
    seat = data.get('seat')
    random.shuffle(state['decks'][seat])
    push_log(f"玩家 {seat} 洗牌。")
    broadcast_state()

@socketio.on('qishu')
def on_qishu(data):
    # 奇树：将双方手牌洗乱后放到各自牌堆底部，不改变其他牌堆顺序
    for s in ('A','B'):
        hand = state['hands'][s]
        if hand:
            random.shuffle(hand)
            state['decks'][s].extend(hand)
            state['hands'][s] = []
    push_log('奇树发动：双方手牌已洗乱并放入牌堆底部。')
    broadcast_state()

@socketio.on('move_card')
def on_move(data):
    # generic movement request from client: expects {seat, from_zone, to_zone, card, extra}
    # zones: deck, hand, bench_i (bench_0..bench_4), active, stadium, discard
    seat = data.get('seat')
    frm = data.get('from_zone')
    to = data.get('to_zone')
    card = data.get('card')
    # remove card from source
    removed = False
    def remove_from_list(lst, card):
        try:
            lst.remove(card)
            return True
        except ValueError:
            return False
    if frm == 'hand':
        removed = remove_from_list(state['hands'][seat], card)
    elif frm == 'deck':
        removed = remove_from_list(state['decks'][seat], card)
    elif frm.startswith('bench_'):
        idx = int(frm.split('_')[1])
        if state['benches'][seat][idx] == card:
            state['benches'][seat][idx] = None
            removed = True
    elif frm == 'active':
        if state['active'][seat] == card:
            state['active'][seat] = None
            removed = True
    elif frm == 'stadium':
        if state['stadium'][seat] == card:
            state['stadium'][seat] = None
            removed = True
    elif frm == 'discard':
        removed = remove_from_list(state['discards'][seat], card)
    if not removed:
        emit('action_resp', {'ok':False, 'error':'无法从指定区域移除该卡'});
        return
    # add to destination
    if to == 'hand':
        state['hands'][seat].append(card)
    elif to == 'deck_top':
        state['decks'][seat].insert(0, card)
    elif to == 'deck_bottom':
        state['decks'][seat].append(card)
    elif to.startswith('bench_'):
        idx = int(to.split('_')[1])
        state['benches'][seat][idx] = card
    elif to == 'active':
        state['active'][seat] = card
    elif to == 'stadium':
        state['stadium'][seat] = card
    elif to == 'discard':
        state['discards'][seat].append(card)
    push_log(f"玩家 {seat} 将卡牌 {card} 从 {frm} 移动到 {to}。")
    broadcast_state()
    emit('action_resp', {'ok':True})

@socketio.on('chat')
def on_chat(data):
    seat = data.get('seat')
    text = data.get('text')
    push_log(f"[{seat}] {text}")
    broadcast_state()

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
    .card { display:inline-block; padding:6px 8px; border-radius:6px; border:1px solid #666; margin:4px; cursor:grab; user-select:none }
    #controls { margin-bottom:8px }
    #right { width:360px; border-left:1px solid #ddd; padding:8px; box-sizing:border-box }
    #log { height:60vh; overflow:auto; background:#f7f7f7; padding:6px; border:1px solid #ddd }
    #chat { margin-top:8px }
    button { margin-right:6px }
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

<script src="https://cdn.socket.io/4.6.1/socket.io.min.js"></script>
<script>
const socket = io();
let mySeat = null;

function $(id){return document.getElementById(id)}

socket.on('connected', d=>console.log(d));
socket.on('state_update', s=>{
  // update UI based on state
  // bench lists
  const benchA = $('benchA'); benchA.innerHTML=''; s.benches.A.forEach((c,i)=>{const el=document.createElement('div'); el.className='card'; el.textContent=c||'[空]'; el.dataset.seat='A'; el.dataset.zone='bench_'+i; el.draggable = c? true:false; benchA.appendChild(el);});
  const benchB = $('benchB'); benchB.innerHTML=''; s.benches.B.forEach((c,i)=>{const el=document.createElement('div'); el.className='card'; el.textContent=c||'[空]'; el.dataset.seat='B'; el.dataset.zone='bench_'+i; el.draggable = c? true:false; benchB.appendChild(el);});

  // active/stadium/discard
  const activeA = $('activeA'); activeA.innerHTML=''; if(s.active.A){const el=document.createElement('div'); el.className='card'; el.textContent=s.active.A; el.dataset.seat='A'; el.dataset.zone='active'; el.draggable=true; activeA.appendChild(el)} else activeA.textContent='[空]';
  const activeB = $('activeB'); activeB.innerHTML=''; if(s.active.B){const el=document.createElement('div'); el.className='card'; el.textContent=s.active.B; el.dataset.seat='B'; el.dataset.zone='active'; el.draggable=true; activeB.appendChild(el)} else activeB.textContent='[空]';
  const stadiumA = $('stadiumA'); stadiumA.innerHTML=''; if(s.stadium.A){const el=document.createElement('div'); el.className='card'; el.textContent=s.stadium.A; el.dataset.seat='A'; el.dataset.zone='stadium'; el.draggable=true; stadiumA.appendChild(el)} else stadiumA.textContent='[空]';
  const stadiumB = $('stadiumB'); stadiumB.innerHTML=''; if(s.stadium.B){const el=document.createElement('div'); el.className='card'; el.textContent=s.stadium.B; el.dataset.seat='B'; el.dataset.zone='stadium'; el.draggable=true; stadiumB.appendChild(el)} else stadiumB.textContent='[空]';

  const discardA = $('discardA'); discardA.innerHTML=''; s.discards.A.forEach(c=>{const el=document.createElement('div'); el.className='card'; el.textContent=c; el.dataset.seat='A'; el.dataset.zone='discard'; el.draggable=true; discardA.appendChild(el)});
  const discardB = $('discardB'); discardB.innerHTML=''; s.discards.B.forEach(c=>{const el=document.createElement('div'); el.className='card'; el.textContent=c; el.dataset.seat='B'; el.dataset.zone='discard'; el.draggable=true; discardB.appendChild(el)});

  // hand: note: client should not show opponent's hand content
  $('handCountA').textContent = s.hands_count.A;
  $('handCountB').textContent = s.hands_count.B;
  if(mySeat){
    // request private hand right after state update so UI shows actual cards promptly
    socket.emit('request_hand', {seat: mySeat});
  }

  // log
  const logEl = $('log'); logEl.innerHTML = s.log.map(l=>'<div>'+l+'</div>').join(''); logEl.scrollTop = logEl.scrollHeight;
});

// request full private hand when server responds
socket.on('private_hand', d=>{
  if(d.seat==mySeat){
    const handEl = document.getElementById('hand'+d.seat);
    handEl.innerHTML='';
    d.cards.forEach(c=>{const el=document.createElement('div'); el.className='card'; el.textContent=c; el.draggable=true; el.dataset.zone='hand'; el.dataset.seat=d.seat; handEl.appendChild(el);});
  }
});

// Simple helpers for UI actions
$('joinBtn').onclick = ()=>{
  const seat = $('seatSel').value;
  if(!seat) return alert('请选择座位');
  socket.emit('join', {seat});
  mySeat = seat;
  $('mySeat').textContent = seat;
  // request private hand immediately after sitting
  socket.emit('request_hand', {seat: mySeat});
}
$('leaveBtn').onclick = ()=>{ socket.emit('leave_seat',{seat:mySeat}); mySeat=null; $('mySeat').textContent='-'; }
$('startBtn').onclick = ()=>{ if(!mySeat) return alert('请先坐下'); socket.emit('start_game',{seat:mySeat}); }
$('drawA').onclick = ()=>{ socket.emit('draw',{seat:'A', n:1}); }
$('drawB').onclick = ()=>{ socket.emit('draw',{seat:'B', n:1}); }
$('shuffleA').onclick = ()=>{ socket.emit('shuffle_deck',{seat:'A'}); }
$('shuffleB').onclick = ()=>{ socket.emit('shuffle_deck',{seat:'B'}); }
$('qishuBtn').onclick = ()=>{ socket.emit('qishu',{}); }

$('sendChat').onclick = ()=>{ const t = $('chatInput').value; if(!mySeat) return alert('请先坐下'); socket.emit('chat',{seat:mySeat, text:t}); $('chatInput').value=''; }

// Drag & drop: use native dragstart to populate dataTransfer

document.addEventListener('dragstart', function(ev){
  const el = ev.target.closest('.card');
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
    // some browsers may restrict writing to dataTransfer in synthetic events
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
    // security: only allow moving your own cards or moving public cards
    if(d.seat !== mySeat){
      if(d.zone !== 'hand'){
        // allow moving public cards
      } else {
        return alert('不能移动对手手牌');
      }
    }
    // determine to_zone string
    socket.emit('move_card', {seat: d.seat, from_zone: d.zone, to_zone: to_zone, card: d.card});
  });
}

setupDrop('activeA','active','A');
setupDrop('activeB','active','B');
setupDrop('benchA','bench_0','A'); // bench area simplified: always bench_0 for demo
setupDrop('benchB','bench_0','B');
setupDrop('stadiumA','stadium','A');
setupDrop('stadiumB','stadium','B');
setupDrop('discardA','discard','A');
setupDrop('discardB','discard','B');

// Request periodic refresh of private hand: for demo, the server doesn't send hand contents due to privacy; but we implement a small poll via a custom event
setInterval(()=>{
  if(mySeat){ socket.emit('request_hand', {seat: mySeat}); }
}, 2000);

</script>
</body>
</html>
'''

@app.route('/')
def index():
    return render_template_string(INDEX_HTML)

# Implement request_hand to send private hand contents back to requesting client only
@socketio.on('request_hand')
def on_request_hand(data):
    seat = data.get('seat')
    sid = request.sid
    # verify requester matches seat occupant
    if state['players'].get(seat) == sid:
        # send private hand only to that client
        socketio.emit('private_hand', {'seat':seat, 'cards': state['hands'][seat]}, to=sid)

if __name__ == '__main__':
    print('Starting PTCG demo server on port 9088...')
    socketio.run(app, host='0.0.0.0', port=9088)
