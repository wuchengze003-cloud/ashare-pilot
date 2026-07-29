import type { Metadata } from "next";
import "./globals.css";
import Analytics from "./Analytics";

const siteUrl = process.env.NEXT_PUBLIC_SITE_URL ?? "http://localhost:3100";

export const metadata: Metadata = {
  metadataBase: new URL(siteUrl),
  title: {
    default: "A股量化信号台",
    template: "%s · A股量化信号台",
  },
  description: "严格区分研究候选与生产策略的 A 股量化信号控制台。未通过生产门禁时不发布开仓信号。",
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
    siteName: "A股量化信号台",
    title: "A股量化信号台",
    description: "通过样本外、成本、交易约束与生产门禁后才发布确定性信号。",
    url: "/",
    images: [
      {
        url: "/social-card.png",
        width: 1200,
        height: 630,
        alt: "A股量化信号台",
      },
    ],
  },
  twitter: {
    card: "summary_large_image",
    title: "A股量化信号台",
    description: "严格生产准入的 A 股量化信号控制台。",
    images: ["/social-card.png"],
  },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>
        {children}
        <footer className="site-footer">
          <div className="container footer-inner">
            <span>A股量化信号台 · 研究与生产严格隔离</span>
            <span className="muted">数据仅供参考，不构成投资建议</span>
          </div>
        </footer>
        <Analytics />
      </body>
    </html>
  );
}
