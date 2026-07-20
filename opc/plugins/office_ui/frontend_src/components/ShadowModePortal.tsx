import React, { useState, useEffect } from 'react';

interface Task {
  id: string;
  title: string;
  description: string;
  status: string;
  priority: number;
  assigned_to: string;
}

interface User {
  username: string;
  name: string;
  role_id: string;
  access_level: string;
}

export const ShadowModePortal: React.FC = () => {
  const [token, setToken] = useState<string>(localStorage.getItem('opc_contractor_jwt') || '');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [user, setUser] = useState<User | null>(null);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [selectedTaskId, setSelectedTaskId] = useState<string>('');
  const [notes, setNotes] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [statusMsg, setStatusMsg] = useState<{ type: 'error' | 'success'; text: string } | null>(null);

  useEffect(() => {
    if (token) {
      fetchTasks();
    }
  }, [token]);

  const handleLogin = async (e: React.FormEvent) => {
    e.preventDefault();
    setStatusMsg(null);
    try {
      const res = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      });
      const data = await res.json();
      if (!res.ok) {
        setStatusMsg({ type: 'error', text: data.error || 'Authentication failed' });
        return;
      }
      setToken(data.token);
      setUser(data.user);
      localStorage.setItem('opc_contractor_jwt', data.token);
      setStatusMsg({ type: 'success', text: `Welcome back, ${data.user.name}!` });
    } catch (err: any) {
      setStatusMsg({ type: 'error', text: err.message || 'Login failed' });
    }
  };

  const fetchTasks = async () => {
    try {
      const res = await fetch(`/api/contractor/tasks?token=${token}`);
      const data = await res.json();
      if (res.ok) {
        setTasks(data.tasks || []);
        if (data.user) setUser(data.user);
        if (data.tasks.length > 0) setSelectedTaskId(data.tasks[0].id);
      }
    } catch (err) {
      console.error('Failed to load contractor tasks', err);
    }
  };

  const handleSubmitDeliverable = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedTaskId) {
      setStatusMsg({ type: 'error', text: 'Please select a task' });
      return;
    }
    if (!notes && !file) {
      setStatusMsg({ type: 'error', text: 'Deliverable notes or file upload required' });
      return;
    }

    const formData = new FormData();
    formData.append('task_id', selectedTaskId);
    formData.append('notes', notes);
    if (file) formData.append('file', file);

    try {
      const res = await fetch('/api/contractor/submit', {
        method: 'POST',
        headers: { Authorization: `Bearer ${token}` },
        body: formData,
      });
      const data = await res.json();
      if (!res.ok) {
        setStatusMsg({ type: 'error', text: data.error || 'Submission failed' });
        return;
      }
      setStatusMsg({ type: 'success', text: 'Deliverable submitted! Pipeline execution resuming.' });
      setNotes('');
      setFile(null);
      fetchTasks();
    } catch (err: any) {
      setStatusMsg({ type: 'error', text: err.message || 'Submission error' });
    }
  };

  if (!token) {
    return (
      <div className="max-w-md mx-auto my-12 p-8 bg-slate-900 border border-slate-800 rounded-xl shadow-2xl text-slate-100">
        <h2 className="text-2xl font-bold mb-2 text-cyan-400">Shadow Mode Portal</h2>
        <p className="text-sm text-slate-400 mb-6">Contractor Authentication</p>

        {statusMsg && (
          <div className={`p-3 mb-4 rounded-md text-sm ${statusMsg.type === 'error' ? 'bg-red-900/40 text-red-300 border border-red-800' : 'bg-emerald-900/40 text-emerald-300 border border-emerald-800'}`}>
            {statusMsg.text}
          </div>
        )}

        <form onSubmit={handleLogin} className="space-y-4">
          <div>
            <label className="block text-xs uppercase tracking-wider text-slate-400 mb-1">Username</label>
            <input
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              className="w-full px-3 py-2 bg-slate-800 border border-slate-700 rounded-md text-slate-100 focus:outline-none focus:border-cyan-500"
              required
            />
          </div>
          <div>
            <label className="block text-xs uppercase tracking-wider text-slate-400 mb-1">Password</label>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full px-3 py-2 bg-slate-800 border border-slate-700 rounded-md text-slate-100 focus:outline-none focus:border-cyan-500"
              required
            />
          </div>
          <button
            type="submit"
            className="w-full py-2.5 bg-cyan-600 hover:bg-cyan-500 font-semibold text-white rounded-md transition-colors shadow-md"
          >
            Log In
          </button>
        </form>
      </div>
    );
  }

  const selectedTask = tasks.find((t) => t.id === selectedTaskId);

  return (
    <div className="max-w-5xl mx-auto my-6 p-6 bg-slate-900 border border-slate-800 rounded-xl shadow-xl text-slate-100">
      <div className="flex justify-between items-center pb-4 border-b border-slate-800">
        <div>
          <h2 className="text-xl font-bold text-cyan-400">Assigned Contractor Work Items</h2>
          <p className="text-xs text-slate-400">Role: <span className="text-slate-200">{user?.role_id}</span> | Account: <span className="text-slate-200">{user?.name}</span></p>
        </div>
        <button
          onClick={() => {
            localStorage.removeItem('opc_contractor_jwt');
            setToken('');
          }}
          className="px-3 py-1.5 text-xs bg-slate-800 hover:bg-slate-700 text-slate-300 rounded border border-slate-700"
        >
          Log Out
        </button>
      </div>

      {statusMsg && (
        <div className={`mt-4 p-3 rounded-md text-sm ${statusMsg.type === 'error' ? 'bg-red-900/40 text-red-300 border border-red-800' : 'bg-emerald-900/40 text-emerald-300 border border-emerald-800'}`}>
          {statusMsg.text}
        </div>
      )}

      {tasks.length === 0 ? (
        <div className="my-8 p-6 bg-slate-950/60 border border-slate-850 text-center text-slate-400 rounded-lg">
          No pending work items requiring deliverables for role <span className="text-cyan-300 font-mono">'{user?.role_id}'</span>.
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-6 mt-6">
          <div className="space-y-3">
            <label className="block text-xs font-semibold text-slate-400 uppercase tracking-wider">Select Task</label>
            <div className="space-y-2">
              {tasks.map((t) => (
                <div
                  key={t.id}
                  onClick={() => setSelectedTaskId(t.id)}
                  className={`p-3 rounded-lg border cursor-pointer transition-all ${
                    t.id === selectedTaskId ? 'bg-slate-800 border-cyan-500 text-cyan-200 shadow' : 'bg-slate-950/40 border-slate-800 hover:border-slate-700 text-slate-300'
                  }`}
                >
                  <div className="font-semibold text-sm">{t.title}</div>
                  <div className="text-xs text-slate-500 font-mono mt-1">{t.id}</div>
                </div>
              ))}
            </div>
          </div>

          <div className="md:col-span-2 bg-slate-950/60 border border-slate-800 rounded-lg p-5">
            {selectedTask ? (
              <form onSubmit={handleSubmitDeliverable} className="space-y-4">
                <div>
                  <h3 className="text-lg font-bold text-slate-100">{selectedTask.title}</h3>
                  <p className="text-xs text-slate-400 mt-1">{selectedTask.description}</p>
                </div>

                <div className="pt-2">
                  <label className="block text-xs font-semibold text-slate-400 uppercase tracking-wider mb-1">Deliverable Notes / Summary</label>
                  <textarea
                    rows={4}
                    value={notes}
                    onChange={(e) => setNotes(e.target.value)}
                    placeholder="Describe the technical work completed..."
                    className="w-full px-3 py-2 bg-slate-900 border border-slate-800 rounded-md text-sm text-slate-200 focus:outline-none focus:border-cyan-500"
                  />
                </div>

                <div>
                  <label className="block text-xs font-semibold text-slate-400 uppercase tracking-wider mb-1">Attach Deliverable File</label>
                  <input
                    type="file"
                    onChange={(e) => setFile(e.target.files ? e.target.files[0] : null)}
                    className="w-full text-xs text-slate-400 file:mr-3 file:py-1.5 file:px-3 file:rounded file:border-0 file:text-xs file:font-semibold file:bg-slate-800 file:text-slate-200 hover:file:bg-slate-700"
                  />
                </div>

                <button
                  type="submit"
                  className="w-full py-2.5 bg-emerald-600 hover:bg-emerald-500 text-white font-bold rounded-md shadow transition-colors"
                >
                  Submit Deliverable & Resume Pipeline
                </button>
              </form>
            ) : (
              <div className="text-slate-500 text-sm">Select a task from the list to submit deliverable.</div>
            )}
          </div>
        </div>
      )}
    </div>
  );
};
