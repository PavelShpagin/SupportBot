import { useRouter } from 'next/router';
import { useEffect, useState } from 'react';
import { format } from 'date-fns';
import { MessageSquare, CheckCircle, AlertCircle, Image as ImageIcon } from 'lucide-react';

interface CaseData {
  case_id: string;
  problem_title: string;
  problem_summary: string;
  solution_summary: string;
  status: string;
  created_at: string;
  tags: string[];
  evidence: {
    message_id: string;
    ts: number;
    sender_hash: string;
    content_text: string;
    images: string[];
  }[];
}

export default function CasePage() {
  const router = useRouter();
  const { id } = router.query;
  const [data, setData] = useState<CaseData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!id) return;

    const apiUrl = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';
    
    fetch(`${apiUrl}/api/cases/${id}`)
      .then(async (res) => {
        if (!res.ok) {
            const text = await res.text();
            throw new Error(`API Error ${res.status}: ${res.statusText} ${text ? `(${text})` : ''}`);
        }
        return res.json();
      })
      .then((data) => {
        setData(data);
        setLoading(false);
      })
      .catch((err) => {
        setError(err.message);
        setLoading(false);
      });
  }, [id]);

  if (loading) return (
    <div className="min-h-screen bg-white flex items-center justify-center">
      <div className="flex flex-col items-center gap-4">
        <div className="w-12 h-12 border-4 border-blue-500 border-t-transparent rounded-full animate-spin"></div>
        <p className="text-gray-600 text-sm">Loading case...</p>
      </div>
    </div>
  );
  if (error) return <div className="p-8 text-center text-red-500">Error: {error}</div>;
  if (!data) return null;

  const apiUrl = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

  return (
    <div className="min-h-screen bg-gray-50 py-8 px-4 sm:px-6 lg:px-8">
      <div className="max-w-3xl mx-auto space-y-8">
        {/* Header */}
        <div className="bg-white shadow rounded-lg p-6 border-l-4 border-blue-500">
          <div className="flex items-center justify-between mb-4">
            <h1 className="text-2xl font-bold text-gray-900">{data.problem_title}</h1>
            <span className={`px-3 py-1 rounded-full text-sm font-medium ${
              data.status === 'solved' ? 'bg-green-100 text-green-800' : 'bg-yellow-100 text-yellow-800'
            }`}>
              {data.status.toUpperCase()}
            </span>
          </div>
          <div className="text-sm text-gray-500 mb-4">
            Case ID: {data.case_id} • {data.created_at ? format(new Date(data.created_at), 'PPP p') : ''}
          </div>
          <div className="flex flex-wrap gap-2 mb-4">
            {data.tags.map(tag => (
              <span key={tag} className="bg-gray-100 text-gray-600 px-2 py-1 rounded text-xs">
                #{tag}
              </span>
            ))}
          </div>
        </div>

        {/* Problem & Solution */}
        <div className="grid gap-6 md:grid-cols-2">
          <div className="bg-white shadow rounded-lg p-6">
            <h2 className="flex items-center text-lg font-semibold text-gray-900 mb-3">
              <AlertCircle className="w-5 h-5 mr-2 text-red-500" />
              Problem
            </h2>
            <p className="text-gray-700 whitespace-pre-wrap">{data.problem_summary}</p>
          </div>
          
          <div className="bg-white shadow rounded-lg p-6">
            <h2 className="flex items-center text-lg font-semibold text-gray-900 mb-3">
              <CheckCircle className="w-5 h-5 mr-2 text-green-500" />
              Solution
            </h2>
            <p className="text-gray-700 whitespace-pre-wrap">{data.solution_summary}</p>
          </div>
        </div>

        {/* Chat Transcript */}
        <div className="bg-white shadow rounded-lg overflow-hidden">
          <div className="bg-gray-50 px-6 py-4 border-b border-gray-200">
            <h2 className="flex items-center text-lg font-semibold text-gray-900">
              <MessageSquare className="w-5 h-5 mr-2 text-blue-500" />
              Conversation History
            </h2>
          </div>
          <div className="divide-y divide-gray-200">
            {data.evidence.map((msg) => (
              <div key={msg.message_id} id={msg.message_id} className="p-6 hover:bg-gray-50 transition-colors">
                <div className="flex items-start space-x-3">
                  <div className="flex-shrink-0">
                    <div className="w-8 h-8 rounded-full bg-blue-100 flex items-center justify-center text-blue-600 text-xs font-bold">
                      {msg.sender_hash.substring(0, 2)}
                    </div>
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center justify-between mb-1">
                      <p className="text-sm font-medium text-gray-900 truncate">
                        User {msg.sender_hash.substring(0, 6)}...
                      </p>
                      <p className="text-xs text-gray-500">
                        {format(new Date(msg.ts), 'MMM d, p')}
                      </p>
                    </div>
                    <p className="text-gray-800 whitespace-pre-wrap">{msg.content_text}</p>
                    
                    {msg.images && msg.images.length > 0 && (
                      <div className="mt-4 grid grid-cols-2 gap-2 sm:grid-cols-3">
                        {msg.images.map((img, idx) => (
                          <a key={idx} href={`${apiUrl}${img}`} target="_blank" rel="noopener noreferrer" className="block relative aspect-square bg-gray-100 rounded-lg overflow-hidden border border-gray-200 hover:opacity-90 transition-opacity">
                            {/* eslint-disable-next-line @next/next/no-img-element */}
                            <img 
                              src={`${apiUrl}${img}`} 
                              alt="Attachment" 
                              className="object-cover w-full h-full"
                            />
                            <div className="absolute bottom-1 right-1 bg-black/50 text-white p-1 rounded">
                              <ImageIcon className="w-3 h-3" />
                            </div>
                          </a>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
