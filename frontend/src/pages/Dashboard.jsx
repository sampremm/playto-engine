import React, { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { v4 as uuidv4 } from 'uuid';
import api from '../api';
import {
  LogOut, Wallet, Activity, CheckCircle, Clock, XCircle,
  ArrowRightLeft, Send, Webhook, Plus, RefreshCw, Trash2
} from 'lucide-react';

export default function Dashboard() {
  const [balance, setBalance] = useState({ available_rupees: 0, held_rupees: 0 });
  const [payouts, setPayouts] = useState([]);
  const [ledger, setLedger] = useState([]);
  const [webhookEndpoints, setWebhookEndpoints] = useState([]);
  const [webhookDeliveries, setWebhookDeliveries] = useState([]);
  const [amount, setAmount] = useState('');
  const [bankAccount, setBankAccount] = useState('');
  const [webhookUrl, setWebhookUrl] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [activeTab, setActiveTab] = useState('payouts');
  const navigate = useNavigate();

  const fetchBalance  = async () => { try { const r = await api.get('/api/v1/merchants/balance/'); setBalance(r.data); } catch(e) { if(e.response?.status===401) handleLogout(); } };
  const fetchPayouts  = async () => { try { const r = await api.get('/api/v1/payouts/list/'); setPayouts(r.data); } catch(e) {} };
  const fetchLedger   = async () => { try { const r = await api.get('/api/v1/merchants/ledger/'); setLedger(r.data); } catch(e) {} };
  const fetchWebhooks = async () => {
    try {
      const [ep, dl] = await Promise.all([
        api.get('/api/v1/webhooks/endpoints/'),
        api.get('/api/v1/webhooks/deliveries/'),
      ]);
      setWebhookEndpoints(ep.data);
      setWebhookDeliveries(dl.data);
    } catch(e) {}
  };

  useEffect(() => {
    // Initial load — fetch everything once
    fetchBalance(); fetchPayouts(); fetchLedger(); fetchWebhooks();
    // Poll only balance + payouts every 60s (payout status changes are slow)
    // Ledger and webhooks refresh only on user actions — not on a timer
    const interval = setInterval(() => {
      fetchBalance(); fetchPayouts();
      // If we're on the webhooks tab, also poll deliveries every 10s
      // to show processing state (QUEUED -> SENT)
      if (activeTab === 'webhooks') {
        fetchWebhooks();
      }
    }, activeTab === 'webhooks' ? 10000 : 60000);
    return () => clearInterval(interval);
  }, [activeTab]);

  const handleLogout = () => { localStorage.removeItem('token'); navigate('/login'); };

  const handlePayout = async (e) => {
    e.preventDefault(); setLoading(true); setError('');
    try {
      await api.post('/api/v1/payouts/', {
        amount_paise: Math.round(parseFloat(amount) * 100),
        bank_account_id: bankAccount
      }, { headers: { 'Idempotency-Key': uuidv4() } });
      setAmount(''); setBankAccount('');
      fetchBalance(); fetchPayouts(); fetchLedger();
    } catch(err) {
      setError(err.response?.data?.error || 'Failed to create payout');
    } finally { setLoading(false); }
  };

  const handleRegisterWebhook = async (e) => {
    e.preventDefault(); setLoading(true); setError('');
    try {
      await api.post('/api/v1/webhooks/endpoints/', { url: webhookUrl });
      setWebhookUrl(''); fetchWebhooks();
    } catch(err) {
      setError(err.response?.data?.error || 'Failed to register webhook');
    } finally { setLoading(false); }
  };

  const handleDeleteWebhook = async (id) => {
    if (!window.confirm('Delete this webhook endpoint?')) return;
    try {
      await api.delete('/api/v1/webhooks/endpoints/', { data: { id } });
      fetchWebhooks();
    } catch(err) {
      alert('Failed to delete endpoint');
    }
  };

  const getStatusBadge = (status) => {
    const styles = {
      COMPLETED: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30',
      SENT:      'bg-emerald-500/10 text-emerald-400 border-emerald-500/30',
      FAILED:    'bg-red-500/10 text-red-400 border-red-500/30',
      PROCESSING:'bg-amber-500/10 text-amber-400 border-amber-500/30',
      RETRYING:  'bg-orange-500/10 text-orange-400 border-orange-500/30',
      PENDING:   'bg-slate-700/50 text-slate-300 border-slate-600',
      QUEUED:    'bg-blue-500/10 text-blue-400 border-blue-500/30',
    };
    const icons = {
      COMPLETED: <CheckCircle className="w-3.5 h-3.5 mr-1.5" />,
      SENT:      <CheckCircle className="w-3.5 h-3.5 mr-1.5" />,
      FAILED:    <XCircle className="w-3.5 h-3.5 mr-1.5" />,
      PROCESSING:<Activity className="w-3.5 h-3.5 mr-1.5 animate-pulse" />,
      RETRYING:  <RefreshCw className="w-3.5 h-3.5 mr-1.5 animate-spin" />,
      PENDING:   <Clock className="w-3.5 h-3.5 mr-1.5" />,
      QUEUED:    <Clock className="w-3.5 h-3.5 mr-1.5" />,
    };
    return (
      <span className={`inline-flex items-center px-3 py-1 rounded-full text-xs font-bold tracking-wide border ${styles[status] || styles.PENDING}`}>
        {icons[status]}{status}
      </span>
    );
  };

  const tabs = [
    { id: 'payouts', label: 'Recent Payouts', icon: <ArrowRightLeft className="w-4 h-4 mr-2" /> },
    { id: 'ledger',  label: 'Ledger',          icon: <Wallet className="w-4 h-4 mr-2" /> },
    { id: 'webhooks',label: 'Webhooks',         icon: <Webhook className="w-4 h-4 mr-2" /> },
  ];

  return (
    <div className="min-h-screen relative overflow-hidden pb-20">
      <div className="absolute top-0 right-0 w-[600px] h-[600px] bg-primary/10 rounded-full mix-blend-screen filter blur-[150px] pointer-events-none" />
      <div className="absolute bottom-0 left-0 w-[500px] h-[500px] bg-teal-500/10 rounded-full mix-blend-screen filter blur-[150px] pointer-events-none" />

      <div className="max-w-7xl mx-auto p-6 relative z-10 animate-fade-in">
        {/* Header */}
        <header className="flex justify-between items-center mb-10 pb-6 border-b border-slate-800">
          <div className="flex items-center space-x-3">
            <div className="p-2.5 bg-gradient-to-br from-primary/20 to-indigo-500/20 rounded-xl border border-primary/30">
              <Activity className="w-6 h-6 text-primary" />
            </div>
            <h1 className="text-2xl font-bold text-white tracking-tight">
              Playto <span className="text-slate-500 font-light">Dashboard</span>
            </h1>
          </div>
          <button onClick={handleLogout} className="flex items-center px-4 py-2 text-sm font-medium text-slate-400 hover:text-white bg-slate-800/50 hover:bg-slate-800 rounded-lg transition-all duration-200 border border-transparent hover:border-slate-700">
            <LogOut className="w-4 h-4 mr-2" /> Sign Out
          </button>
        </header>

        {/* Top Cards */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6 mb-10">
          <div className="card glass-panel group">
            <div className="flex items-center text-slate-400 mb-4 font-medium">
              <Wallet className="w-5 h-5 mr-2 text-primary" /> Available Balance
            </div>
            <div className="text-5xl font-black text-transparent bg-clip-text bg-gradient-to-br from-white to-slate-400 group-hover:to-white transition-colors duration-300">
              ₹{balance.available_rupees?.toLocaleString() ?? 0}
            </div>
          </div>

          <div className="card glass-panel group">
            <div className="flex items-center text-slate-400 mb-4 font-medium">
              <Activity className="w-5 h-5 mr-2 text-warning" /> Held in Processing
            </div>
            <div className="text-5xl font-black text-transparent bg-clip-text bg-gradient-to-br from-white to-slate-400 group-hover:to-white transition-colors duration-300">
              ₹{balance.held_rupees?.toLocaleString() ?? 0}
            </div>
          </div>

          <div className="card glass-panel bg-gradient-to-br from-slate-900/80 to-slate-800/80">
            <div className="flex items-center mb-5 text-white font-semibold">
              <Send className="w-5 h-5 mr-2 text-primary" /> Request Payout
            </div>
            {error && <div className="text-red-400 text-xs mb-4 bg-red-500/10 p-2 rounded border border-red-500/20">{error}</div>}
            <form onSubmit={handlePayout} className="space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <input type="number" placeholder="Amount (₹)" value={amount}
                  onChange={e => setAmount(e.target.value)} className="input-field text-sm" required min="1" />
                <input type="text" placeholder="Account ID" value={bankAccount}
                  onChange={e => setBankAccount(e.target.value)} className="input-field text-sm" required />
              </div>
              <button type="submit" disabled={loading} className="btn-primary w-full text-sm py-2.5">
                {loading ? 'Processing...' : 'Withdraw Funds'}
              </button>
            </form>
          </div>
        </div>

        {/* Tabs */}
        <div className="flex space-x-1 mb-6 bg-slate-900/50 p-1 rounded-xl border border-slate-800 w-fit">
          {tabs.map(t => (
            <button key={t.id} onClick={() => setActiveTab(t.id)}
              className={`flex items-center px-5 py-2.5 rounded-lg text-sm font-medium transition-all duration-200 ${
                activeTab === t.id
                  ? 'bg-slate-700 text-white shadow-lg'
                  : 'text-slate-400 hover:text-white hover:bg-slate-800/60'
              }`}>
              {t.icon}{t.label}
            </button>
          ))}
        </div>

        {/* Tab Panels */}
        {activeTab === 'payouts' && (
          <div className="card glass-panel !p-0">
            <div className="p-6 border-b border-slate-700/50 bg-slate-800/20">
              <h3 className="font-semibold text-lg text-white flex items-center">
                <ArrowRightLeft className="w-5 h-5 mr-2 text-slate-400" /> Recent Payouts
              </h3>
            </div>
            <div className="overflow-x-auto max-h-[500px]">
              <table className="w-full text-left text-sm text-slate-300">
                <thead className="text-xs text-slate-400 uppercase bg-slate-900/50 sticky top-0 backdrop-blur-md">
                  <tr>
                    {['ID','Amount','Date','Status'].map(h => (
                      <th key={h} className="px-6 py-4 font-semibold tracking-wider">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-700/50">
                  {payouts.map(p => (
                    <tr key={p.id} className="hover:bg-slate-800/40 transition-colors group">
                      <td className="px-6 py-4 font-mono text-xs text-slate-500 group-hover:text-slate-300">{p.id?.slice(0,8)}...</td>
                      <td className="px-6 py-4 font-bold text-white">₹{(p.amount_rupees||0).toLocaleString()}</td>
                      <td className="px-6 py-4 text-slate-400">{new Date(p.created_at).toLocaleString(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'})}</td>
                      <td className="px-6 py-4">{getStatusBadge(p.status)}</td>
                    </tr>
                  ))}
                  {payouts.length === 0 && (
                    <tr><td colSpan="4" className="px-6 py-12 text-center text-slate-500">No payouts yet. Request your first withdrawal above.</td></tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {activeTab === 'ledger' && (
          <div className="card glass-panel !p-0">
            <div className="p-6 border-b border-slate-700/50 bg-slate-800/20">
              <h3 className="font-semibold text-lg text-white flex items-center">
                <Wallet className="w-5 h-5 mr-2 text-slate-400" /> Ledger Transactions
              </h3>
            </div>
            <div className="overflow-x-auto max-h-[500px]">
              <table className="w-full text-left text-sm text-slate-300">
                <thead className="text-xs text-slate-400 uppercase bg-slate-900/50 sticky top-0 backdrop-blur-md">
                  <tr>
                    {['Type','Amount','Description','Date'].map(h => (
                      <th key={h} className="px-6 py-4 font-semibold tracking-wider">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-700/50">
                  {ledger.map(entry => (
                    <tr key={entry.id} className="hover:bg-slate-800/40 transition-colors">
                      <td className="px-6 py-4">
                        <span className={`px-2.5 py-1 rounded text-xs font-bold tracking-wide ${entry.amount_paise > 0 ? 'bg-emerald-500/10 text-emerald-400' : 'bg-amber-500/10 text-amber-400'}`}>
                          {entry.entry_type}
                        </span>
                      </td>
                      <td className={`px-6 py-4 font-bold ${entry.amount_paise > 0 ? 'text-emerald-400' : 'text-amber-400'}`}>
                        {entry.amount_paise > 0 ? '+' : ''}₹{(entry.amount_paise / 100).toLocaleString()}
                      </td>
                      <td className="px-6 py-4 text-xs text-slate-400 truncate max-w-[180px]">{entry.description}</td>
                      <td className="px-6 py-4 text-slate-400 whitespace-nowrap">
                        {new Date(entry.created_at).toLocaleString(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'})}
                      </td>
                    </tr>
                  ))}
                  {ledger.length === 0 && (
                    <tr><td colSpan="4" className="px-6 py-12 text-center text-slate-500">No ledger entries found.</td></tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {activeTab === 'webhooks' && (
          <div className="space-y-6">
            {/* Register Endpoint */}
            <div className="card glass-panel">
              <h3 className="font-semibold text-lg text-white flex items-center mb-5">
                <Plus className="w-5 h-5 mr-2 text-primary" /> Register Webhook Endpoint
              </h3>
              <form onSubmit={handleRegisterWebhook} className="flex gap-3">
                <input type="url" placeholder="https://your-server.com/webhook"
                  value={webhookUrl} onChange={e => setWebhookUrl(e.target.value)}
                  className="input-field text-sm flex-1" required />
                <button type="submit" disabled={loading} className="btn-primary text-sm px-6 whitespace-nowrap">
                  {loading ? 'Registering...' : 'Register URL'}
                </button>
              </form>
              {webhookEndpoints.length > 0 && (
                <div className="mt-4 space-y-2">
                  {webhookEndpoints.map(ep => (
                    <div key={ep.id} className="flex items-center justify-between p-3 bg-slate-800/40 rounded-lg border border-slate-700/50 group/item">
                      <div className="flex items-center space-x-3 truncate">
                        <span className="font-mono text-xs text-slate-300 truncate">{ep.url}</span>
                        <span className={`text-xs px-2 py-0.5 rounded ${ep.is_active ? 'bg-emerald-500/10 text-emerald-400' : 'bg-slate-700 text-slate-400'}`}>
                          {ep.is_active ? 'Active' : 'Inactive'}
                        </span>
                      </div>
                      <button 
                        onClick={() => handleDeleteWebhook(ep.id)}
                        className="p-1.5 text-slate-500 hover:text-red-400 hover:bg-red-500/10 rounded transition-all opacity-0 group-hover/item:opacity-100"
                        title="Delete Endpoint"
                      >
                        <Trash2 className="w-4 h-4" />
                      </button>
                    </div>
                  ))}
                </div>
              )}
            </div>

            {/* Delivery History */}
            <div className="card glass-panel !p-0">
              <div className="p-6 border-b border-slate-700/50 bg-slate-800/20 flex justify-between items-center">
                <h3 className="font-semibold text-lg text-white flex items-center">
                  <Webhook className="w-5 h-5 mr-2 text-slate-400" /> Delivery History
                  <span className="ml-3 text-xs text-slate-500 font-normal hidden sm:inline">QUEUED → PROCESSING → RETRYING → SENT/FAILED</span>
                </h3>
                <button 
                  onClick={fetchWebhooks}
                  className="p-1.5 hover:bg-slate-700 rounded-lg transition-colors text-slate-400 hover:text-white"
                  title="Refresh Deliveries"
                >
                  <RefreshCw className="w-4 h-4" />
                </button>
              </div>
              <div className="overflow-x-auto max-h-[400px]">
                <table className="w-full text-left text-sm text-slate-300">
                  <thead className="text-xs text-slate-400 uppercase bg-slate-900/50 sticky top-0 backdrop-blur-md">
                    <tr>
                      {['Event','Endpoint','Status','Attempts','HTTP','Sent At'].map(h => (
                        <th key={h} className="px-5 py-4 font-semibold tracking-wider">{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-700/50">
                    {webhookDeliveries.map(d => (
                      <tr key={d.id} className="hover:bg-slate-800/40 transition-colors">
                        <td className="px-5 py-4 font-mono text-xs text-blue-400">{d.event_type}</td>
                        <td className="px-5 py-4 text-xs text-slate-400 truncate max-w-[150px]">{d.endpoint_url}</td>
                        <td className="px-5 py-4">{getStatusBadge(d.status)}</td>
                        <td className="px-5 py-4 text-center text-slate-300">{d.attempt_count}/{d.max_attempts}</td>
                        <td className="px-5 py-4">
                          {d.last_http_status ? (
                            <span className={`font-mono text-xs ${d.last_http_status < 300 ? 'text-emerald-400' : 'text-red-400'}`}>
                              {d.last_http_status}
                            </span>
                          ) : <span className="text-slate-600">—</span>}
                        </td>
                        <td className="px-5 py-4 text-slate-400 text-xs whitespace-nowrap">
                          {d.delivered_at ? new Date(d.delivered_at).toLocaleString(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '—'}
                        </td>
                      </tr>
                    ))}
                    {webhookDeliveries.length === 0 && (
                      <tr><td colSpan="6" className="px-6 py-12 text-center text-slate-500">No webhook deliveries yet. Register an endpoint and create a payout.</td></tr>
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
