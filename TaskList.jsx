import React, { useState, useMemo } from 'react';

const STATUS_OPTIONS = [
  { value: 'open', label: 'Открыта' },
  { value: 'in_progress', label: 'В работе' },
  { value: 'pending', label: 'Ожидает' },
  { value: 'done', label: 'Выполнена' },
];

const PRIORITY_OPTIONS = [
  { value: 'high', label: 'Высокий' },
  { value: 'medium', label: 'Средний' },
  { value: 'low', label: 'Низкий' },
];

const PRIORITY_COLORS = {
  high: '#ef4444',
  medium: '#f59e0b',
  low: '#22c55e',
};

const STATUS_COLORS = {
  open: '#3b82f6',
  in_progress: '#8b5cf6',
  pending: '#6b7280',
  done: '#22c55e',
};

function TaskList({ items: initialItems = [] }) {
  const [tasks, setTasks] = useState(initialItems);
  const [statusFilter, setStatusFilter] = useState('');
  const [priorityFilter, setPriorityFilter] = useState('');

  const filteredTasks = useMemo(() => {
    return tasks.filter((task) => {
      const matchStatus = !statusFilter || task.status === statusFilter;
      const matchPriority = !priorityFilter || task.priority === priorityFilter;
      return matchStatus && matchPriority;
    });
  }, [tasks, statusFilter, priorityFilter]);

  const handleStatusChange = (taskId, newStatus) => {
    setTasks((prev) =>
      prev.map((task) =>
        task.id === taskId ? { ...task, status: newStatus } : task
      )
    );
  };

  return (
    <div className="task-list">
      <div className="filters">
        <div className="filter-group">
          <label htmlFor="status-filter">Статус:</label>
          <select
            id="status-filter"
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
          >
            <option value="">Все</option>
            {STATUS_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        </div>
        <div className="filter-group">
          <label htmlFor="priority-filter">Приоритет:</label>
          <select
            id="priority-filter"
            value={priorityFilter}
            onChange={(e) => setPriorityFilter(e.target.value)}
          >
            <option value="">Все</option>
            {PRIORITY_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        </div>
      </div>

      <div className="tasks-grid">
        {filteredTasks.map((task) => (
          <div key={task.id} className="task-card">
            <div className="task-header">
              <h3 className="task-name">{task.name}</h3>
              <span
                className="priority-badge"
                style={{ backgroundColor: PRIORITY_COLORS[task.priority] || '#6b7280' }}
              >
                {PRIORITY_OPTIONS.find((p) => p.value === task.priority)?.label ?? task.priority}
              </span>
            </div>
            <p className="task-description">{task.description}</p>
            <div className="task-footer">
              <label className="status-label">
                Статус:
                <select
                  value={task.status}
                  onChange={(e) => handleStatusChange(task.id, e.target.value)}
                  style={{
                    borderColor: STATUS_COLORS[task.status] || '#6b7280',
                  }}
                >
                  {STATUS_OPTIONS.map((opt) => (
                    <option key={opt.value} value={opt.value}>
                      {opt.label}
                    </option>
                  ))}
                </select>
              </label>
            </div>
          </div>
        ))}
      </div>

      {filteredTasks.length === 0 && (
        <p className="no-tasks">Задачи не найдены</p>
      )}

      <style>{`
        .task-list {
          max-width: 900px;
          margin: 0 auto;
          padding: 1.5rem;
          font-family: system-ui, -apple-system, sans-serif;
        }

        .filters {
          display: flex;
          gap: 1.5rem;
          margin-bottom: 1.5rem;
          flex-wrap: wrap;
        }

        .filter-group {
          display: flex;
          align-items: center;
          gap: 0.5rem;
        }

        .filter-group label {
          font-weight: 500;
          color: #374151;
        }

        .filter-group select {
          padding: 0.5rem 0.75rem;
          border: 1px solid #d1d5db;
          border-radius: 6px;
          font-size: 0.875rem;
          background: #fff;
          cursor: pointer;
        }

        .filter-group select:focus {
          outline: none;
          border-color: #3b82f6;
          box-shadow: 0 0 0 2px rgba(59, 130, 246, 0.2);
        }

        .tasks-grid {
          display: grid;
          grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
          gap: 1rem;
        }

        .task-card {
          background: #fff;
          border: 1px solid #e5e7eb;
          border-radius: 8px;
          padding: 1rem;
          box-shadow: 0 1px 3px rgba(0, 0, 0, 0.08);
          transition: box-shadow 0.2s;
        }

        .task-card:hover {
          box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1);
        }

        .task-header {
          display: flex;
          justify-content: space-between;
          align-items: flex-start;
          gap: 0.5rem;
          margin-bottom: 0.5rem;
        }

        .task-name {
          margin: 0;
          font-size: 1rem;
          font-weight: 600;
          color: #111827;
          flex: 1;
          min-width: 0;
        }

        .priority-badge {
          flex-shrink: 0;
          padding: 0.2rem 0.5rem;
          border-radius: 4px;
          font-size: 0.7rem;
          font-weight: 600;
          color: #fff;
        }

        .task-description {
          margin: 0 0 1rem;
          font-size: 0.875rem;
          color: #6b7280;
          line-height: 1.4;
        }

        .task-footer {
          padding-top: 0.75rem;
          border-top: 1px solid #f3f4f6;
        }

        .status-label {
          display: flex;
          align-items: center;
          gap: 0.5rem;
          font-size: 0.875rem;
          font-weight: 500;
          color: #374151;
        }

        .status-label select {
          padding: 0.35rem 0.5rem;
          border: 2px solid #d1d5db;
          border-radius: 6px;
          font-size: 0.8rem;
          background: #fff;
          cursor: pointer;
        }

        .status-label select:focus {
          outline: none;
        }

        .no-tasks {
          text-align: center;
          color: #6b7280;
          padding: 2rem;
        }
      `}</style>
    </div>
  );
}

export default TaskList;
