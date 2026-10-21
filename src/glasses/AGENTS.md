# Loci 地灵

城市解谜游戏 Loci 在 Rokid Glasses 上的玩家视角。

- 打开后显示第几站和谜面，每 5 秒从 Loci 中继（`src/relay`）的 `GET /api/v1/state` 刷新一次。
- 点一下镜腿：拍一张照片发给中继（`POST /api/v1/photo`），镜片上显示「找对了没有」；找对了就用语音讲出这一站的故事。
- 往上滑：对眼镜说一句话，识别成文字发给中继（`POST /api/v1/talk`），地灵回答并念出来。
- 往下滑：这一站解开后进入下一站（`POST /api/v1/next`），并念出新谜面。
- 中继地址写在 `config.js`（不进仓库，参考 `config.example.js`），必须是 HTTPS。

它只显示和拍照，不自己判断；判断都在中继。
