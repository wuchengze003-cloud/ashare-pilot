"use client";

import { useState } from "react";
import {
  paginateBuySignalHistory,
  summarizeBuySignalHistory,
  type BuySignalHistoryRow,
} from "@/lib/buySignalHistory";

const PAGE_SIZES = [10, 20, 50];

function numberOrDash(value: number | null, digits = 2) {
  return value == null || Number.isNaN(value) ? "暂无" : value.toFixed(digits);
}

function pctOrDash(value: number | null, digits = 1) {
  if (value == null || Number.isNaN(value)) return "暂无";
  return `${value > 0 ? "+" : ""}${value.toFixed(digits)}%`;
}

export function BuySignalHistoryTable({
  rows,
  archiveDates,
}: {
  rows: BuySignalHistoryRow[];
  archiveDates: string[];
}) {
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(PAGE_SIZES[0]);
  const summary = summarizeBuySignalHistory(rows);
  const paginated = paginateBuySignalHistory(rows, page, pageSize);
  const firstDate = archiveDates.at(-1) ?? null;
  const lastDate = archiveDates.at(0) ?? null;

  return (
    <div className="theme-panel signal-history-panel">
      <div className="theme-title signal-history-title">
        <strong>信号归档</strong>
        <span>
          {archiveDates.length} 个交易日
          {firstDate && lastDate ? ` · ${firstDate} 至 ${lastDate}` : ""}
          {" · 现价来自最新快照"}
        </span>
      </div>

      <div className="signal-history-overview" aria-label="历史买入信号总览">
        <div><span>买入信号</span><strong>{summary.totalSignals}</strong></div>
        <div><span>有效样本</span><strong>{summary.validSignals}</strong></div>
        <div>
          <span>上涨比例</span>
          <strong className={summary.winRatePct == null ? "" : summary.winRatePct >= 50 ? "pos" : "neg"}>
            {pctOrDash(summary.winRatePct)}
          </strong>
        </div>
        <div>
          <span>平均涨跌</span>
          <strong className={summary.averageChangePct == null ? "" : summary.averageChangePct >= 0 ? "pos" : "neg"}>
            {pctOrDash(summary.averageChangePct)}
          </strong>
        </div>
      </div>

      <div className="table-wrap compact-table signal-history-table-wrap">
        <table className="signal-history-table">
          <thead>
            <tr>
              <th>信号日</th>
              <th>代码</th>
              <th>名称</th>
              <th>主题</th>
              <th className="num">信号价</th>
              <th className="num">现价</th>
              <th className="num">信号后涨跌</th>
              <th>理由</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 ? (
              <tr><td colSpan={8} className="muted">暂无历史信号归档</td></tr>
            ) : paginated.rows.map((signal) => (
              <tr key={`${signal.signalDate}-${signal.symbol}`}>
                <td className="mono">{signal.signalDate}</td>
                <td className="mono">{signal.symbol}</td>
                <td>{signal.name ?? "名称未收录"}</td>
                <td>{signal.theme ?? "主题未收录"}</td>
                <td className="num" title={signal.signalPriceDate ?? undefined}>
                  {numberOrDash(signal.signalPrice)}
                </td>
                <td className="num" title={signal.currentAsOf ?? undefined}>
                  {numberOrDash(signal.currentPrice)}
                </td>
                <td className={`num ${signal.changePct == null ? "muted" : signal.changePct >= 0 ? "pos" : "neg"}`}>
                  {pctOrDash(signal.changePct)}
                </td>
                <td className="muted signal-reason">{signal.rationale}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="signal-history-pagination">
        <span>
          {rows.length === 0 ? "0 条" : `${paginated.startIndex + 1}-${paginated.endIndex} / 共 ${rows.length} 条`}
        </span>
        <label>
          每页
          <select
            value={pageSize}
            onChange={(event) => {
              setPageSize(Number(event.target.value));
              setPage(1);
            }}
            aria-label="每页显示条数"
          >
            {PAGE_SIZES.map((size) => <option key={size} value={size}>{size}</option>)}
          </select>
        </label>
        <button
          type="button"
          className="secondary"
          disabled={paginated.page <= 1}
          onClick={() => setPage((current) => Math.max(1, current - 1))}
        >
          上一页
        </button>
        <span className="signal-history-page">第 {paginated.page} / {paginated.totalPages} 页</span>
        <button
          type="button"
          className="secondary"
          disabled={paginated.page >= paginated.totalPages}
          onClick={() => setPage((current) => Math.min(paginated.totalPages, current + 1))}
        >
          下一页
        </button>
      </div>
    </div>
  );
}
