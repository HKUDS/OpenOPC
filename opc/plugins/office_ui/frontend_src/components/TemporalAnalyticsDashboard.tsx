import React, { useState, useEffect } from 'react';

interface GlobalMetric {
  time_bucket: string;
  completed_tasks: number;
  total_active_tasks: number;
  avg_outcome_score: number;
}

interface Changelog {
  id: string;
  timestamp: string;
  event_type: string;
  actor_id: string;
  description: string;
  impact_score: number;
}

export const TemporalAnalyticsDashboard: React.FC = () => {
  const [interval, setInterval] = useState<'weekly' | 'daily'>('weekly');
  const [globalMetrics, setGlobalMetrics] = useState<GlobalMetric[]>([]);
  const [teamMetrics, setTeamMetrics] = useState<Record<string, any[]>>({});
  const [indivMetrics, setIndivMetrics] = useState<Record<string, any[]>>({});
  const [selectedTeam, setSelectedTeam] = useState<string>('');
  const [selectedActor, setSelectedActor] = useState<string>('');
  const [changelogs, setChangelogs] = useState<Changelog[]>([]);
  const [searchQuery, setSearchQuery] = useState('');

  useEffect(() => {
    fetchAnalytics();
  }, [interval]);

  useEffect(() => {
    fetchChangelogs();
  }, [searchQuery]);

  const getAuthHeaders = (): Record<string, string> => {
    const token = localStorage.getItem('opc_token') || sessionStorage.getItem('opc_token') || '';
    return token ? { 'Authorization': `Bearer ${token}` } : {};
  };

  const fetchAnalytics = async () => {
    try {
      const res = await fetch(`/api/analytics/temporal_performance?interval=${interval}`, {
        headers: getAuthHeaders(),
      });
      const data = await res.json();
      if (res.ok) {
        setGlobalMetrics(data.global || []);
        setTeamMetrics(data.team || {});
        setIndivMetrics(data.individual || {});

        const teams = Object.keys(data.team || {});
        if (teams.length > 0 && !selectedTeam) setSelectedTeam(teams[0]);

        const actors = Object.keys(data.individual || {});
        if (actors.length > 0 && !selectedActor) setSelectedActor(actors[0]);
      }
    } catch (err) {
      console.error('Failed to load performance analytics', err);
    }
  };

  const fetchChangelogs = async () => {
    try {
      const res = await fetch(`/api/analytics/changelogs?limit=50&search=${encodeURIComponent(searchQuery)}`, {
        headers: getAuthHeaders(),
      });
      const data = await res.json();
      if (res.ok) {
        setChangelogs(data.changelogs || []);
      }
    } catch (err) {
      console.error('Failed to fetch changelogs', err);
    }
  };

  return (
    <div className="max-w-6xl mx-auto my-6 p-6 bg-slate-900 border border-slate-800 rounded-xl shadow-xl text-slate-100">
      <div className="flex justify-between items-center pb-4 border-b border-slate-800">
        <div>
          <h2 className="text-xl font-bold text-cyan-400">Time-Machine Analytics & Velocity</h2>
          <p className="text-xs text-slate-400">Historical performance aggregation across Global, Team, and Individual levels</p>
        </div>
        <div className="flex bg-slate-800 p-1 rounded-lg border border-slate-700">
          <button
            onClick={() => setInterval('weekly')}
            className={`px-3 py-1 text-xs font-medium rounded ${interval === 'weekly' ? 'bg-cyan-600 text-white shadow' : 'text-slate-400 hover:text-slate-200'}`}
          >
            Weekly
          </button>
          <button
            onClick={() => setInterval('daily')}
            className={`px-3 py-1 text-xs font-medium rounded ${interval === 'daily' ? 'bg-cyan-600 text-white shadow' : 'text-slate-400 hover:text-slate-200'}`}
          >
            Daily
          </button>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6 mt-6">
        {/* Global Organization Velocity */}
        <div className="bg-slate-950/60 border border-slate-800 rounded-lg p-5">
          <h3 className="text-sm font-bold text-slate-200 mb-3">Global Organization Velocity</h3>
          {globalMetrics.length === 0 ? (
            <p className="text-xs text-slate-500 py-6 text-center">No global performance entries yet.</p>
          ) : (
            <div className="space-y-3">
              {globalMetrics.map((m, idx) => (
                <div key={idx} className="flex justify-between items-center p-2.5 bg-slate-900 border border-slate-800 rounded">
                  <span className="text-xs font-mono text-cyan-300">{m.time_bucket}</span>
                  <div className="flex gap-4 text-xs">
                    <span className="text-emerald-400 font-semibold">{m.completed_tasks} Completed</span>
                    <span className="text-slate-400">{m.total_active_tasks} Total</span>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Team Slices */}
        <div className="bg-slate-950/60 border border-slate-800 rounded-lg p-5">
          <div className="flex justify-between items-center mb-3">
            <h3 className="text-sm font-bold text-slate-200">Team / Role Slices</h3>
            {Object.keys(teamMetrics).length > 0 && (
              <select
                value={selectedTeam}
                onChange={(e) => setSelectedTeam(e.target.value)}
                className="bg-slate-900 border border-slate-700 text-xs text-slate-200 px-2 py-1 rounded focus:outline-none"
              >
                {Object.keys(teamMetrics).map((t) => (
                  <option key={t} value={t}>
                    {t}
                  </option>
                ))}
              </select>
            )}
          </div>
          {!selectedTeam || !teamMetrics[selectedTeam] ? (
            <p className="text-xs text-slate-500 py-6 text-center">No team slice metrics recorded yet.</p>
          ) : (
            <div className="space-y-2">
              {teamMetrics[selectedTeam].map((tm, idx) => (
                <div key={idx} className="flex justify-between items-center p-2.5 bg-slate-900 border border-slate-800 rounded">
                  <span className="text-xs font-mono text-slate-300">{tm.time_bucket}</span>
                  <span className="text-xs text-cyan-400 font-semibold">{tm.completed_tasks} Tasks Completed</span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      <hr className="my-6 border-slate-800" />

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        {/* Individual Performance */}
        <div className="bg-slate-950/60 border border-slate-800 rounded-lg p-5">
          <div className="flex justify-between items-center mb-3">
            <h3 className="text-sm font-bold text-slate-200">Individual Employee Slices</h3>
            {Object.keys(indivMetrics).length > 0 && (
              <select
                value={selectedActor}
                onChange={(e) => setSelectedActor(e.target.value)}
                className="bg-slate-900 border border-slate-700 text-xs text-slate-200 px-2 py-1 rounded focus:outline-none"
              >
                {Object.keys(indivMetrics).map((a) => (
                  <option key={a} value={a}>
                    {a}
                  </option>
                ))}
              </select>
            )}
          </div>
          {!selectedActor || !indivMetrics[selectedActor] ? (
            <p className="text-xs text-slate-500 py-6 text-center">No individual metrics recorded yet.</p>
          ) : (
            <div className="space-y-2">
              {indivMetrics[selectedActor].map((im, idx) => (
                <div key={idx} className="flex justify-between items-center p-2.5 bg-slate-900 border border-slate-800 rounded">
                  <span className="text-xs font-mono text-slate-300">{im.time_bucket}</span>
                  <div className="flex gap-3 text-xs">
                    <span className="text-purple-400">{im.event_count} Events</span>
                    <span className="text-emerald-400 font-semibold">Impact: {im.impact_score}</span>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Organizational Changelog Feed */}
        <div className="bg-slate-950/60 border border-slate-800 rounded-lg p-5">
          <h3 className="text-sm font-bold text-slate-200 mb-2">Organizational Changelog Feed</h3>
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="Search changelog events..."
            className="w-full px-3 py-1.5 bg-slate-900 border border-slate-800 rounded text-xs text-slate-200 mb-3 focus:outline-none focus:border-cyan-500"
          />
          <div className="space-y-2 max-h-64 overflow-y-auto pr-1">
            {changelogs.length === 0 ? (
              <p className="text-xs text-slate-500 py-4 text-center">No matching changelog events.</p>
            ) : (
              changelogs.map((c) => (
                <div key={c.id} className="p-2 bg-slate-900 border border-slate-850 rounded text-xs">
                  <div className="flex justify-between text-slate-400 text-[10px]">
                    <span className="font-mono">{new Date(c.timestamp).toLocaleString()}</span>
                    <span className="text-cyan-400 font-semibold">{c.event_type}</span>
                  </div>
                  <div className="text-slate-200 mt-1 font-medium">{c.description}</div>
                  <div className="text-[10px] text-slate-500 mt-1">Actor: {c.actor_id} | Impact: {c.impact_score}</div>
                </div>
              ))
            )}
          </div>
        </div>
      </div>
    </div>
  );
};
