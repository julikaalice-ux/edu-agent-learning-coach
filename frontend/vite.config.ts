import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    host: true,
    proxy: {
      // 本地小代理：backend/proxy.py → 托管 agent（凭据不出现在浏览器）
      "/turn": "http://127.0.0.1:8787",
      // 互动图和题目配图：页面用 /artifact/<token> 显示，本地也走代理去取
      "/artifact": "http://127.0.0.1:8787",
    },
  },
});
