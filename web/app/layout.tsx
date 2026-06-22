import type { Metadata } from "next";
import "./globals.css";

const siteUrl = process.env.NEXT_PUBLIC_SITE_URL ?? "http://localhost:3100";

export const metadata: Metadata = {
  metadataBase: new URL(siteUrl),
  title: {
    default: "硅基文明消费股交易系统",
    template: "%s · 硅基文明消费股交易系统",
  },
  description: "Dashboard 规则评分、行情通道、规则测算目标价、A 股主题股票池、实时信号与回测系统。",
  icons: {
    icon: [
      { url: "/icon.svg", type: "image/svg+xml" },
      { url: "/icon-512.png", sizes: "512x512", type: "image/png" },
    ],
    apple: [{ url: "/apple-touch-icon.png", sizes: "180x180", type: "image/png" }],
  },
  openGraph: {
    type: "website",
    locale: "zh_CN",
    siteName: "硅基文明消费股交易系统",
    title: "硅基文明消费股交易系统",
    description: "A 股硅基文明消费主题股票池、规则测算目标价、实时规则信号与策略回测。",
    url: "/",
    images: [
      {
        url: "/social-card.png",
        width: 1200,
        height: 630,
        alt: "硅基文明消费股交易系统社交分享卡片",
      },
    ],
  },
  twitter: {
    card: "summary_large_image",
    title: "硅基文明消费股交易系统",
    description: "A 股硅基文明消费主题股票池、规则测算目标价、实时规则信号与策略回测。",
    images: ["/social-card.png"],
  },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
