import React, { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import api from '../api';
import { Lock, Mail, Activity, ArrowRight } from 'lucide-react';

export default function Login() {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();

  const handleLogin = async (e) => {
    e.preventDefault();
    setLoading(true);
    try {
      const res = await api.post('/api/v1/auth/login/', { username: email, password });
      localStorage.setItem('token', res.data.access);
      navigate('/dashboard');
    } catch (err) {
      setError('Invalid credentials. Try arjun@demo.com / demo123');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="flex items-center justify-center min-h-screen relative overflow-hidden">
      {/* Decorative background blobs */}
      <div className="absolute top-0 left-1/4 w-96 h-96 bg-primary/30 rounded-full mix-blend-screen filter blur-[120px] animate-pulse-slow"></div>
      <div className="absolute bottom-0 right-1/4 w-[500px] h-[500px] bg-indigo-500/20 rounded-full mix-blend-screen filter blur-[150px] animate-pulse-slow" style={{animationDelay: '1.5s'}}></div>

      <div className="card glass-panel w-full max-w-md mx-4 animate-slide-up z-10 p-8 sm:p-10">
        <div className="text-center mb-10">
          <div className="flex justify-center mb-6">
            <div className="p-4 bg-gradient-to-br from-primary/20 to-indigo-500/20 rounded-2xl border border-primary/30 shadow-[0_0_20px_rgba(59,130,246,0.2)]">
              <Activity className="w-8 h-8 text-primary" />
            </div>
          </div>
          <h1 className="text-4xl font-extrabold bg-clip-text text-transparent bg-gradient-to-r from-blue-400 via-indigo-300 to-primary mb-3">
            Playto Pay
          </h1>
          <p className="text-slate-400 font-medium tracking-wide">Enterprise payout infrastructure.</p>
        </div>
        
        {error && (
          <div className="bg-error/10 border border-error/30 text-error px-4 py-3 rounded-xl mb-6 flex items-center text-sm animate-fade-in">
            {error}
          </div>
        )}
        
        <form onSubmit={handleLogin} className="space-y-5">
          <div className="group">
            <label className="block text-sm font-medium text-slate-400 mb-1.5 transition-colors group-focus-within:text-primary">Email Address</label>
            <div className="relative">
              <div className="absolute inset-y-0 left-0 pl-4 flex items-center pointer-events-none">
                <Mail className="h-5 w-5 text-slate-500 group-focus-within:text-primary transition-colors" />
              </div>
              <input 
                type="email" 
                value={email}
                onChange={e => setEmail(e.target.value)}
                className="input-field pl-12" 
                placeholder="demo@playto.com"
                required 
              />
            </div>
          </div>
          
          <div className="group">
            <label className="block text-sm font-medium text-slate-400 mb-1.5 transition-colors group-focus-within:text-primary">Password</label>
            <div className="relative">
              <div className="absolute inset-y-0 left-0 pl-4 flex items-center pointer-events-none">
                <Lock className="h-5 w-5 text-slate-500 group-focus-within:text-primary transition-colors" />
              </div>
              <input 
                type="password" 
                value={password}
                onChange={e => setPassword(e.target.value)}
                className="input-field pl-12" 
                placeholder="••••••••"
                required 
              />
            </div>
          </div>
          
          <button type="submit" disabled={loading} className="btn-primary w-full mt-8 flex items-center justify-center py-3.5 text-lg">
            {loading ? 'Authenticating...' : (
              <>Sign In <ArrowRight className="ml-2 w-5 h-5" /></>
            )}
          </button>
        </form>
        
        <div className="mt-8 text-center bg-slate-800/50 rounded-xl p-4 border border-slate-700/50">
          <p className="text-xs text-slate-400 uppercase tracking-wider mb-2 font-semibold">Demo Accounts</p>
          <div className="flex flex-col space-y-1 text-sm text-slate-300">
            <span>arjun@demo.com &bull; priya@demo.com</span>
            <span className="text-slate-500">Password: <span className="font-mono text-slate-300">demo123</span></span>
          </div>
        </div>
      </div>
    </div>
  );
}
