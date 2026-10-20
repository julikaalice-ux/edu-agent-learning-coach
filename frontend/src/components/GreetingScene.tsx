import { useIntroVideo } from "../hooks/useIntroVideo";
import introPoster from "/intro-poster.jpg";
import introVideo from "/intro.mp4";

// 本地压缩版（原片 30MB → 1MB）：一打开就能开始播放，不再等远端加载。
// HF 平台不允许仓库里有视频文件，本地文件缺失时回退到远端原片。
const VIDEO_URL = introVideo;
const REMOTE_VIDEO_URL =
  "https://d8j0ntlcm91z4.cloudfront.net/user_38xzZboKViGWJOttwIXH07lWA1P/hf_20260328_083109_283f3553-e28f-428b-a723-d639c617eb2b.mp4";

interface GreetingSceneProps {
  input: string;
  onInput: (value: string) => void;
  onSend: () => void;
}

/** 场景 A · 问候：视频开场只播一次，教练先打招呼，可直接打字。 */
export function GreetingScene({ input, onInput, onSend }: GreetingSceneProps) {
  const videoRef = useIntroVideo(VIDEO_URL);

  return (
    <div className="scene absolute inset-0 overflow-hidden">
      {/* 首帧海报兜底：视频还在缓冲时画面已经在。 */}
      <div
        className="absolute inset-0 bg-cover bg-center"
        style={{ backgroundImage: `url(${introPoster})` }}
      />
      <video
        ref={videoRef}
        className="absolute inset-0 h-full w-full object-cover"
        src={VIDEO_URL}
        poster={introPoster}
        preload="auto"
        muted
        playsInline
        onError={(event) => {
          const video = event.currentTarget;
          if (video.src !== REMOTE_VIDEO_URL) video.src = REMOTE_VIDEO_URL;
        }}
      />
      {/* 按原规格：上下渐层，其余交给素材本身 */}
      <div className="absolute inset-0 bg-gradient-to-b from-paper via-transparent to-paper" />
      <div className="relative z-10 flex h-full flex-col items-center justify-center px-6 text-center">
        <p className="animate-fade-rise text-sm tracking-[0.3em] text-moss">学习教练 · edu-agent</p>
        <h1 className="animate-fade-rise mt-6 max-w-3xl font-display text-4xl leading-snug text-ink sm:text-6xl md:text-7xl">
          此刻 奔赴你的旷野
        </h1>
        <div className="animate-fade-rise-delay mt-8 flex w-full max-w-xl flex-col items-stretch gap-3 sm:flex-row sm:items-center">
          <input
            value={input}
            onChange={(event) => onInput(event.target.value)}
            onKeyDown={(event) => event.key === "Enter" && onSend()}
            placeholder="你好，可以问我任何你想学的知识"
            className="min-w-0 flex-1 rounded-full border border-control bg-white/90 px-6 py-4 text-base shadow-sm focus:border-focus focus:outline-none focus:ring-2 focus:ring-focus/20"
          />
          <button
            onClick={onSend}
            className="rounded-full bg-moss px-8 py-4 font-semibold text-white transition-transform hover:scale-[1.03]"
          >
            发送
          </button>
        </div>
        <p className="animate-fade-rise-delay-2 mt-6 text-sm text-muted">
          向下滚动，或点
          <button onClick={onSend} className="mx-1 text-moss underline underline-offset-4">
            开始体验
          </button>
          进入对话
        </p>
      </div>
    </div>
  );
}
