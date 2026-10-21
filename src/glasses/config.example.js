// Copy to config.js (git-ignored) and put the HTTPS address of your Loci relay here.
// AIUI has no LAN access, so a relay on your laptop needs a tunnel, e.g.
//   cloudflared tunnel --url http://localhost:8787   → https://<random>.trycloudflare.com
export const RELAY = 'https://your-relay.example.com';
