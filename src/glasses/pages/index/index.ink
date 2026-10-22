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
const MAX_UPLOAD = 1200000;                     // bytes; bigger photos are shrunk before upload
const SESSION = Math.random().toString(36).slice(2, 6);

export default {
  data: {
    progress: '',
    riddle: '正在连接地灵…',
    story: '',
    status: '点镜腿或点头：让地灵看 · 说「乐奇」：跟地灵说话',
    solved: false,
    busy: false
  },

  onLoad() {
    this.station = 0;
    this.report(`open ua=${(navigator.userAgent || '').slice(0, 60)}`);
    this.refresh();
    this.poll = setInterval(() => this.refresh(), POLL_MS);   // other devices may solve a station too
    try {
      this.enableWorldAwareness();                             // nod = show the spirit
    } catch (err) {
      this.report(`no world awareness: ${err}`);
    }
  },

  onUnload() {
    clearInterval(this.poll);
    if (this.recognition) this.recognition.abort();
  },

  // One line into the relay log, so problems on the glasses are visible without a console.
  report(msg) {
    fetch(`${RELAY}/api/v1/log?device=${DEVICE}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ msg: `[${SESSION}] ${msg}` })
    }).catch(() => {});
  },

  onVoiceWakeup(event) {                         // "乐奇" / "Hi Rokid" / touch wakeup while Loci is open
    if (event.preventDefault) event.preventDefault();
    this.report(`wakeup ${event.keyword || ''}`);
    this.listen();
  },

  onHeadGesture(event) {
    this.report(`gesture ${event.gesture}`);
    if (event.gesture === 'nod') this.snap();
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
    if (s.station !== this.station) {            // a new station: the spirit reads the riddle aloud
      this.station = s.station;
      this.say(s.riddle);
    }
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
    if (this.data.solved) {                      // tap again after solving = go on
      this.next();
      return;
    }
    this.setData({ busy: true, status: '地灵在看…' });
    let track = null;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ video: true });
      [track] = stream.getVideoTracks();
      const photo = await this.shrink(await this.capture(new ImageCapture(track)));
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
        this.setData({ status: '找到了！再点一下镜腿去下一站' });
        this.say(s.unlocked);
      } else {
        this.setData({ status: v.text || '还没找到，再看看' });
      }
    } catch (err) {
      this.setData({ status: `没拍成：${(err && err.message) || err}` });
      this.report(`snap failed: ${(err && err.message) || err}`);
    } finally {
      if (track) track.stop();
      this.setData({ busy: false });
    }
  },

  // No system preview (it waits for a confirmation); fall back to plain defaults if the settings are refused.
  async capture(imageCapture) {
    try {
      return await imageCapture.takePhoto({ mode: 'telephoto', quality: 'normal', enableSystemPreview: false });
    } catch (err) {
      this.report(`takePhoto telephoto refused: ${(err && err.message) || err}`);
      return await imageCapture.takePhoto();
    }
  },

  // Photos go to the relay through the phone; keep them around a megabyte.
  async shrink(photo) {
    this.report(`photo ${photo.type} ${photo.size}B`);
    if (photo.size <= MAX_UPLOAD) return photo;
    try {
      const bitmap = await createImageBitmap(photo);
      const scale = Math.min(1, 1600 / Math.max(bitmap.width, bitmap.height));
      const canvas = new Canvas(Math.round(bitmap.width * scale), Math.round(bitmap.height * scale));
      canvas.getContext('2d').drawImage(bitmap, 0, 0, canvas.width, canvas.height);
      bitmap.close();
      const small = await canvas.convertToBlob({ type: 'image/jpeg', quality: 0.8 });
      this.report(`shrunk to ${small.size}B`);
      return small;
    } catch (err) {
      this.report(`shrink failed, sending original: ${(err && err.message) || err}`);
      return photo;
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
      this.report(`listen error: ${event.error || event.message || ''}`);
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

  // A tap arrives as Enter + GlobalHook on key-up; swipes never reached key-up in the simulator,
  // so the arrows are also handled on key-down. Every code is reported to learn the real mapping.
  onKeyDown(event) {
    this.report(`keydown ${event.code}`);
    const action = { ArrowUp: 'listen', ArrowDown: 'next' }[event.code];
    if (!action) return;
    if (event.preventDefault) event.preventDefault();
    this[action]();
  },

  onKeyUp(event) {
    this.report(`keyup ${event.code}`);
    if (event.code === 'Enter' || event.code === 'GlobalHook') {
      if (event.preventDefault) event.preventDefault();
      this.snap();
    } else if (event.code === 'ArrowUp' || event.code === 'ArrowDown') {
      if (event.preventDefault) event.preventDefault();   // already handled on key-down
    }                                                       // Backspace keeps its default: back / close
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
