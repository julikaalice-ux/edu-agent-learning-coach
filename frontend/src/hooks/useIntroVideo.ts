import { useEffect, useRef } from "react";

/**
 * 引导页视频：只播放一次，播完定格在最后一帧，不循环。
 * 用户开启「减弱动画」时不自动播放，直接展示首帧与问候层。
 */
export function useIntroVideo(videoUrl: string) {
  const ref = useRef<HTMLVideoElement | null>(null);

  useEffect(() => {
    const video = ref.current;
    if (!video) return;
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduced) {
      video.pause();
      return;
    }
    video.playbackRate = 0.65; // 慢放让画面更静、更「高级」
    void video.play().catch(() => {
      /* 自动播放被浏览器拦截时保持首帧，问候层仍然可读 */
    });
  }, [videoUrl]);

  return ref;
}
