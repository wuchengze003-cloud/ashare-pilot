import type { Metadata } from "next";
import "./globals.css";
import Analytics from "./Analytics";

const siteUrl = process.env.NEXT_PUBLIC_SITE_URL ?? "http://localhost:3100";

export const metadata: Metadata = {
  metadataBase: new URL(siteUrl),
  title: {
    default: "A股量化策略站",
    template: "%s · A股量化策略站",
  },
  description: "多因子量化策略矩阵，覆盖 AI 算力产业链。每日收盘决策、次日开盘执行，多策略同步观察。",
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
    siteName: "A股量化策略站",
    title: "A股量化策略站",
    description: "多因子量化策略矩阵，覆盖 AI 算力产业链。每日收盘决策、次日开盘执行。",
    url: "/",
    images: [
      {
        url: "/social-card.png",
        width: 1200,
        height: 630,
        alt: "A股量化策略站",
      },
    ],
  },
  twitter: {
    card: "summary_large_image",
    title: "A股量化策略站",
    description: "多因子量化策略矩阵，覆盖 AI 算力产业链。",
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
            <span>A股量化策略站 · 多因子回测 · 每日收盘决策</span>
            <span className="muted">数据仅供参考，不构成投资建议</span>
          </div>
        </footer>
        <Analytics />
      </body>
    </html>
  );
}
