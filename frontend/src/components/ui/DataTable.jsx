/**
 * DataTable — dense data grid with sticky header and mono numerals.
 *
 * @param {object} props
 * @param {Array<{key: string, label: string, align?: 'left'|'right'|'center', render?: (value, row) => React.ReactNode}>} props.columns
 * @param {Array<object>} props.data          Rows; `row.id` used as key when present.
 * @param {string} [props.emptyMessage]       Fallback text when data is empty.
 * @param {React.ReactNode} [props.emptyState] Rich empty slot (e.g. <EmptyState/>); wins over emptyMessage.
 * @param {string} [props.maxHeight]          CSS max-height enabling vertical scroll with sticky header.
 * @param {Function} [props.onRowClick]       (row) => void; adds pointer cursor.
 *
 * Numeric columns: set `align: 'right'` — values render in .font-num automatically.
 */
const alignClass = (align) =>
  align === 'right' ? 'text-right' : align === 'center' ? 'text-center' : 'text-left';

const DataTable = ({ columns, data, emptyMessage = 'No data available', emptyState, maxHeight, onRowClick }) => (
  <div className="terminal-card overflow-hidden">
    <div className="overflow-x-auto" style={maxHeight ? { maxHeight, overflowY: 'auto' } : undefined}>
      <table className="w-full border-separate border-spacing-0">
        <thead className="sticky top-0 z-10">
          <tr>
            {columns.map((col) => (
              <th
                key={col.key}
                className={`px-3 py-2 text-3xs font-bold uppercase tracking-wider text-muted bg-surface border-b border-border whitespace-nowrap ${alignClass(col.align)}`}
              >
                {col.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {data.length === 0 ? (
            <tr>
              <td colSpan={columns.length} className="px-4 py-4">
                {emptyState || (
                  <p className="py-6 text-center text-xs text-muted">{emptyMessage}</p>
                )}
              </td>
            </tr>
          ) : (
            data.map((row, i) => (
              <tr
                key={row.id ?? i}
                onClick={onRowClick ? () => onRowClick(row) : undefined}
                className={`group transition-colors duration-100 hover:bg-overlay/50 ${onRowClick ? 'cursor-pointer' : ''}`}
              >
                {columns.map((col) => (
                  <td
                    key={col.key}
                    className={`px-3 py-1.5 text-xs font-num text-text border-b border-border/60 whitespace-nowrap ${alignClass(col.align)}`}
                  >
                    {col.render ? col.render(row[col.key], row) : row[col.key]}
                  </td>
                ))}
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  </div>
);

export default DataTable;
