"use client";

import Script from "next/script";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";

declare global {
  interface Window {
    umami?: {
      track: (name: string, data?: Record<string, string | number | boolean>) => void;
    };
  }
}

const ENGAGEMENT_MILESTONES = new Set([15, 30, 60, 180]);
const SCROLL_MILESTONES = [25, 50, 75, 100];

function currentPath(): string {
  return `${window.location.pathname}${window.location.hash}`;
}

export default function Analytics() {
  const pathname = usePathname();
  const [trackerReady, setTrackerReady] = useState(false);
  const websiteId = process.env.NEXT_PUBLIC_UMAMI_WEBSITE_ID;
  const scriptUrl = process.env.NEXT_PUBLIC_UMAMI_SCRIPT_URL;
  const recorderUrl = process.env.NEXT_PUBLIC_UMAMI_RECORDER_URL;
  const domains = process.env.NEXT_PUBLIC_UMAMI_DOMAINS;
  const replayEnabled = process.env.NEXT_PUBLIC_UMAMI_REPLAY_ENABLED === "1";

  useEffect(() => {
    if (!trackerReady || !window.umami) return;

    let activeSeconds = 0;
    let lastActivityAt = Date.now();
    const sentScroll = new Set<number>();
    const noteActivity = () => {
      lastActivityAt = Date.now();
    };
    const trackScroll = () => {
      noteActivity();
      const scrollable = document.documentElement.scrollHeight - window.innerHeight;
      const depth = scrollable <= 0 ? 100 : Math.min(100, Math.round((window.scrollY / scrollable) * 100));
      for (const milestone of SCROLL_MILESTONES) {
        if (depth >= milestone && !sentScroll.has(milestone)) {
          sentScroll.add(milestone);
          window.umami?.track("scroll_depth", { percent: milestone, path: currentPath() });
        }
      }
    };
    const trackSection = () => {
      window.umami?.track("section_view", { path: currentPath() });
    };

    const timer = window.setInterval(() => {
      const isActive = document.visibilityState === "visible" && Date.now() - lastActivityAt <= 60_000;
      if (!isActive) return;
      activeSeconds += 1;
      if (ENGAGEMENT_MILESTONES.has(activeSeconds)) {
        window.umami?.track("engaged_time", { seconds: activeSeconds, path: currentPath() });
      }
    }, 1_000);

    window.addEventListener("scroll", trackScroll, { passive: true });
    window.addEventListener("pointerdown", noteActivity, { passive: true });
    window.addEventListener("keydown", noteActivity);
    window.addEventListener("hashchange", trackSection);
    trackScroll();

    return () => {
      window.clearInterval(timer);
      window.removeEventListener("scroll", trackScroll);
      window.removeEventListener("pointerdown", noteActivity);
      window.removeEventListener("keydown", noteActivity);
      window.removeEventListener("hashchange", trackSection);
    };
  }, [pathname, trackerReady]);

  if (!websiteId || !scriptUrl) return null;

  return (
    <>
      <Script
        src={scriptUrl}
        strategy="afterInteractive"
        data-website-id={websiteId}
        data-domains={domains || undefined}
        data-exclude-search="true"
        data-do-not-track="true"
        data-performance="true"
        onReady={() => setTrackerReady(true)}
      />
      {trackerReady && replayEnabled && recorderUrl ? (
        <Script src={recorderUrl} strategy="afterInteractive" data-website-id={websiteId} />
      ) : null}
    </>
  );
}
