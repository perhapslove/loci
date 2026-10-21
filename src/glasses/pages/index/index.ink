<script def>
{
  "navigationBarTitleText": "Loci 地灵"
}
</script>

<script setup>
// The relay address lives in /config.js (git-ignored; see config.example.js).
// AIUI has no LAN access, so the relay must be reachable over HTTPS.
import { RELAY } from '../../config.js';

const DEVICE = 'glasses-rokid';
const POLL_MS = 5000;

export default {
  data: {
    progress: '',
    riddle: '正在连接地灵…',
    story: '',
    status: '点镜腿：拍给地灵看 · 上滑：跟地灵说话',
    solved: false,
    busy: false
  },

  onLoad() {
    this.refresh();
    this.poll = setInterval(() => this.refresh(), POLL_MS);   // other devices may solve a station too
  },

  onUnload() {
    clearInterval(this.poll);
    if (this.recognition) this.recognition.abort();
  },

  async call(path, options = {}) {             // Ink's fetch rejects an undefined second argument
    const res = await fetch(`${RELAY}${path}${path.includes('?') ? '&' : '?'}device=${DEVICE}`, options);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  },

  async refresh() {
    if (this.data.busy) return;
    try {
      this.show(await this.call('/api/v1/state'));
    } catch (err) {
      this.setData({ riddle: '连不上地灵', status: String((err && err.message) || err) });
    }
  },

  show(s) {
    const solved = (s.solved || []).includes(s.station);
    this.setData({
      progress: `第 ${s.station} / ${s.total} 站${solved ? ' · 已解开' : ''}`,
      riddle: s.riddle || '',
      story: s.unlocked || '',
      solved
    });
  },

  say(text) {
    if (!text) return;
    try {
      speechSynthesis.speak(new SpeechSynthesisUtterance(text), 'immediate');
    } catch (err) {
      console.log('speak failed', err);
    }
  },

  async snap() {
    if (this.data.busy) return;
    if (this.data.solved) {
      this.setData({ status: '这一站已解开 · 下滑去下一站' });
      return;
    }
    this.setData({ busy: true, status: '地灵在看…' });
    let track = null;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ video: true });
      [track] = stream.getVideoTracks();
      const photo = await new ImageCapture(track).takePhoto({ mode: 'telephoto' });
      track.stop();
      track = null;
      const s = await this.call('/api/v1/photo', {
        method: 'POST',
        headers: { 'Content-Type': 'image/jpeg' },
        body: photo
      });
      const v = s.verdict || {};
      this.show(s);
      if (v.match === true) {
        this.setData({ status: '找到了！下滑去下一站' });
        this.say(s.unlocked);
      } else {
        this.setData({ status: v.text || '还没找到，再看看' });
      }
    } catch (err) {
      this.setData({ status: `没拍成：${(err && err.message) || err}` });
    } finally {
      if (track) track.stop();
      this.setData({ busy: false });
    }
  },

  listen() {
    if (this.data.busy) return;
    const recognition = new SpeechRecognition();
    recognition.lang = 'zh-CN';
    this.recognition = recognition;
    this.setData({ busy: true, status: '地灵在听…' });
    recognition.onresult = (event) => {
      const result = event.results && event.results[0] && event.results[0][0];
      this.ask(result ? result.transcript : '');
    };
    recognition.onerror = (event) => {
      this.setData({ busy: false, status: `没听清：${event.error || event.message || ''}` });
    };
    recognition.onend = () => {
      if (this.data.status === '地灵在听…') this.setData({ busy: false, status: '没听到，上滑再说一次' });
    };
    recognition.start();
  },

  async ask(text) {
    if (!text) {
      this.setData({ busy: false, status: '没听清，上滑再说一次' });
      return;
    }
    this.setData({ status: `你：${text}` });
    try {
      const s = await this.call('/api/v1/talk', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text })
      });
      this.show(s);
      this.setData({ status: `地灵：${s.reply}` });
      this.say(s.reply);
    } catch (err) {
      this.setData({ status: `地灵没回应：${(err && err.message) || err}` });
    } finally {
      this.setData({ busy: false });
    }
  },

  async next() {
    if (this.data.busy) return;
    try {
      const s = await this.call('/api/v1/next', { method: 'POST' });
      this.show(s);
      this.setData({ status: s.moved ? '新的一站' : '先解开这一站' });
      if (s.moved) this.say(s.riddle);
    } catch (err) {
      this.setData({ status: String((err && err.message) || err) });
    }
  },

  onKeyUp(event) {
    console.log('loci key', event.code);       // the Rokid key mapping is undocumented; this shows it in the log
    const actions = { Enter: 'snap', GlobalHook: 'snap', ArrowUp: 'listen', ArrowDown: 'next' };
    const action = actions[event.code];
    if (!action) return;                       // Backspace keeps its default: back / close
    if (event.preventDefault) event.preventDefault();
    this[action]();
  }
}
</script>

<page>
  <view class="container">
    <text class="progress">{{ progress }}</text>
    <text class="riddle">{{ riddle }}</text>
    <text class="story">{{ story }}</text>
    <text class="status">{{ status }}</text>
  </view>
</page>

<style>
.container {
  display: flex;
  flex-direction: column;
  justify-content: center;
  height: 100vh;
  padding: 0 24px;
}

.progress {
  color: #40FF5E;
  font-size: 16px;
  line-height: 20px;
  margin-bottom: 8px;
}

.riddle {
  color: #40FF5E;
  font-size: 22px;
  line-height: 30px;
  margin-bottom: 12px;
}

.story {
  color: #40FF5E;
  font-size: 18px;
  line-height: 26px;
  margin-bottom: 12px;
}

.status {
  color: #40FF5E;
  font-size: 16px;
  line-height: 20px;
}
</style>
