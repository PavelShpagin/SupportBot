import { GetServerSideProps } from 'next';
import Head from 'next/head';
import { format } from 'date-fns';

interface Attachment {
  url: string;
  content_type: string;
}

interface EvidenceMessage {
  message_id: string;
  ts: number;
  sender_hash: string;
  sender_name: string | null;
  content_text: string;
  images: string[];
  attachments?: Attachment[];
}

interface CaseData {
  case_id: string;
  problem_title: string;
  problem_summary: string;
  solution_summary: string;
  status: string;
  created_at: string;
  closed_emoji: string | null;
  tags: string[];
  evidence: EvidenceMessage[];
}

interface Props {
  data: CaseData;
  publicApiUrl: string;
}

export const getServerSideProps: GetServerSideProps<Props> = async (ctx) => {
  const { id } = ctx.params as { id: string };
  const internalUrl = process.env.API_INTERNAL_URL || 'http://signal-bot:8000';
  const publicApiUrl = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

  try {
    const res = await fetch(`${internalUrl}/api/cases/${id}`);
    if (res.status === 404) return { notFound: true };
    if (!res.ok) throw new Error(`API ${res.status}`);
    const data: CaseData = await res.json();
    return { props: { data, publicApiUrl } };
  } catch {
    return { notFound: true };
  }
};

export default function CasePage({ data, publicApiUrl }: Props) {
  const senderOrder: string[] = [];
  data.evidence?.forEach((msg) => {
    if (!senderOrder.includes(msg.sender_hash)) senderOrder.push(msg.sender_hash);
  });

  return (
    <>
      <Head>
        <title>{data.problem_title} | SupportBot</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0" />
        <link rel="icon" type="image/png" href="/supportbot-logo.png" />
        <meta property="og:title" content={data.problem_title} />
        <meta property="og:description" content={data.solution_summary} />
        <meta property="og:type" content="article" />
      </Head>

      <style jsx global>{`
        @import url("https://rsms.me/inter/inter.css");

        :root {
          --signal-blue: #2c6bed;
          --page-bg: #f6f7f9;
          --card-bg: #ffffff;
          --text: #0d0d0d;
          --text-sec: #5c5c5c;
          --border: #d8d8d8;
          --radius: 12px;
          --green: #16a34a;
          --yellow: #ca8a04;
        }

        * { margin: 0; padding: 0; box-sizing: border-box; }

        body {
          font-family: "Inter", -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
          background: var(--page-bg);
          color: var(--text);
          min-height: 100vh;
          padding: 48px 20px;
          -webkit-font-smoothing: antialiased;
        }

        @media (max-width: 520px) {
          body { padding: 24px 12px; }
        }
      `}</style>

      <style jsx>{`
        .shell { max-width: 640px; margin: 0 auto; }

        .card {
          background: var(--card-bg);
          border: 1px solid var(--border);
          border-radius: var(--radius);
          overflow: hidden;
          margin-bottom: 16px;
        }

        header {
          display: flex;
          align-items: center;
          justify-content: space-between;
          padding: 14px 20px;
          border-bottom: 1px solid var(--border);
        }

        .header-left { display: flex; align-items: center; gap: 10px; }
        .logo { width: 28px; height: 28px; }
        .brand { font-size: 15px; font-weight: 600; letter-spacing: -0.02em; }

        .status-badge {
          padding: 4px 10px;
          font-size: 11px;
          font-weight: 600;
          text-transform: uppercase;
          letter-spacing: 0.04em;
          border-radius: 4px;
        }
        .status-solved { background: #dcfce7; color: var(--green); }
        .status-open { background: #fef9c3; color: var(--yellow); }
        .status-archived { background: #f3f4f6; color: #6b7280; }

        main { padding: 24px 20px; }

        h1 { 
          font-size: 20px; 
          font-weight: 700; 
          letter-spacing: -0.025em; 
          margin-bottom: 12px;
          line-height: 1.3;
        }

        .meta {
          font-size: 12px;
          color: var(--text-sec);
          margin-bottom: 16px;
        }

        .tags {
          display: flex;
          flex-wrap: wrap;
          gap: 6px;
          margin-bottom: 20px;
        }

        .tag {
          background: rgba(44, 107, 237, 0.08);
          color: var(--signal-blue);
          padding: 4px 10px;
          border-radius: 4px;
          font-size: 12px;
          font-weight: 500;
        }

        .section-title {
          font-size: 11px;
          font-weight: 600;
          text-transform: uppercase;
          letter-spacing: 0.06em;
          color: var(--text-sec);
          margin-bottom: 10px;
          display: flex;
          align-items: center;
          gap: 6px;
        }

        .section-title svg {
          width: 14px;
          height: 14px;
        }

        .section-content {
          font-size: 15px;
          line-height: 1.6;
          color: var(--text);
          white-space: pre-wrap;
        }

        .problem-section {
          padding-bottom: 20px;
          border-bottom: 1px solid var(--border);
          margin-bottom: 20px;
        }

        .solution-section {
          background: #f0fdf4;
          margin: -24px -20px -24px -20px;
          padding: 20px;
          border-top: 1px solid #bbf7d0;
        }

        .solution-section .section-title {
          color: var(--green);
        }

        .video-transcript-section {
          margin-top: 20px;
          border: 1px solid var(--border);
          border-radius: 8px;
          overflow: hidden;
        }
        .video-transcript-section summary {
          padding: 12px 16px;
          cursor: pointer;
          font-size: 13px;
          font-weight: 600;
          color: var(--text-sec);
          background: var(--page-bg);
          user-select: none;
          list-style: none;
          display: flex;
          align-items: center;
          gap: 8px;
          transition: color 0.15s;
        }
        .video-transcript-section summary::-webkit-details-marker { display: none; }
        .video-transcript-section summary::after {
          content: '▸';
          font-size: 10px;
          margin-left: auto;
          transition: transform 0.15s;
        }
        .video-transcript-section[open] summary::after {
          transform: rotate(90deg);
        }
        .video-transcript-section summary:hover {
          color: var(--text);
        }
        .video-transcript-section summary svg {
          width: 15px;
          height: 15px;
          flex-shrink: 0;
        }
        .video-transcript-body {
          padding: 14px 16px;
          font-size: 14px;
          line-height: 1.65;
          color: var(--text);
          white-space: pre-wrap;
          border-top: 1px solid var(--border);
          background: var(--card-bg);
        }
        .video-transcript-body .vt-label {
          font-size: 11px;
          font-weight: 600;
          text-transform: uppercase;
          letter-spacing: 0.04em;
          color: var(--text-sec);
          margin-bottom: 4px;
        }
        .video-transcript-body .vt-block + .vt-block {
          margin-top: 12px;
          padding-top: 12px;
          border-top: 1px solid var(--border);
        }

        .chat-header {
          padding: 14px 20px;
          border-bottom: 1px solid var(--border);
          background: var(--page-bg);
        }

        .chat-header h2 {
          font-size: 14px;
          font-weight: 600;
          color: var(--text);
          display: flex;
          align-items: center;
          gap: 8px;
        }

        .chat-header h2 svg {
          width: 16px;
          height: 16px;
          color: var(--signal-blue);
        }

        .messages { padding: 0; }

        .message {
          padding: 16px 20px;
          border-bottom: 1px solid var(--border);
        }

        .message:last-child { border-bottom: none; }

        .message-header {
          display: flex;
          align-items: center;
          gap: 10px;
          margin-bottom: 8px;
        }

        .avatar {
          width: 28px;
          height: 28px;
          border-radius: 50%;
          background: var(--signal-blue);
          color: white;
          display: flex;
          align-items: center;
          justify-content: center;
          font-size: 11px;
          font-weight: 600;
          flex-shrink: 0;
        }

        .sender-info { flex: 1; min-width: 0; }

        .sender-name {
          font-size: 13px;
          font-weight: 600;
          color: var(--text);
        }

        .message-time {
          font-size: 11px;
          color: var(--text-sec);
        }

        .message-text {
          font-size: 15px;
          line-height: 1.55;
          color: var(--text);
          white-space: pre-wrap;
          margin-left: 38px;
        }

        .message-images {
          display: flex;
          flex-wrap: wrap;
          gap: 8px;
          margin-top: 12px;
          margin-left: 38px;
        }

        .message-images a {
          display: block;
          max-width: 320px;
          border-radius: 8px;
          overflow: hidden;
          border: 1px solid var(--border);
          transition: opacity 0.15s;
        }

        .message-images a:hover {
          opacity: 0.85;
        }

        .message-images img {
          display: block;
          width: 100%;
          height: auto;
          max-height: 240px;
          object-fit: cover;
        }

        .message-images video {
          display: block;
          width: 100%;
          max-width: 320px;
          max-height: 240px;
          border-radius: 8px;
          border: 1px solid var(--border);
        }

        .file-download {
          display: inline-flex;
          align-items: center;
          gap: 8px;
          padding: 8px 14px;
          background: var(--page-bg);
          border: 1px solid var(--border);
          border-radius: 8px;
          font-size: 13px;
          color: var(--signal-blue);
          text-decoration: none;
          font-weight: 500;
          transition: background 0.15s;
        }

        .file-download:hover {
          background: #ebedf0;
        }

        .file-download svg {
          width: 16px;
          height: 16px;
          flex-shrink: 0;
        }

        .transcript-details {
          margin: 8px 0 8px 52px;
          border: 1px solid var(--border);
          border-radius: 8px;
          background: var(--card-bg);
          overflow: hidden;
        }
        .transcript-details summary {
          padding: 7px 12px;
          cursor: pointer;
          font-weight: 500;
          font-size: 12px;
          color: var(--text-sec);
          background: var(--page-bg);
          user-select: none;
          list-style: none;
        }
        .transcript-details summary::-webkit-details-marker { display: none; }
        .transcript-details summary::before {
          content: '▸ ';
          font-size: 10px;
        }
        .transcript-details[open] summary::before {
          content: '▾ ';
        }
        .transcript-details summary:hover {
          color: var(--text);
        }
        .transcript-content {
          padding: 10px 12px;
          white-space: pre-wrap;
          font-size: 13px;
          line-height: 1.6;
          color: var(--text);
          border-top: 1px solid var(--border);
        }

        .empty-chat {
          padding: 32px 20px;
          text-align: center;
          color: var(--text-sec);
          font-size: 14px;
        }

        .emoji-confirmation {
          padding: 14px 20px;
          border-top: 1px solid #bbf7d0;
          background: #f0fdf4;
          display: flex;
          align-items: center;
          gap: 10px;
          font-size: 13px;
          color: var(--green);
          font-weight: 500;
        }

        .emoji-confirmation .emoji-bubble {
          font-size: 22px;
          line-height: 1;
          filter: drop-shadow(0 1px 2px rgba(0,0,0,.12));
        }

        footer {
          padding: 14px 20px;
          border-top: 1px solid var(--border);
          color: var(--text-sec);
          font-size: 12px;
          text-align: center;
        }

        @media (max-width: 520px) {
          main { padding: 20px 16px; }
          h1 { font-size: 18px; }
          .solution-section {
            margin: -20px -16px -20px -16px;
            padding: 16px;
          }
          .message { padding: 14px 16px; }
          .message-text { margin-left: 0; margin-top: 8px; }
          .message-images { margin-left: 0; }
        }
      `}</style>

      <div className="shell">
        <div className="card">
          <header>
            <a href="/" className="header-left" style={{ textDecoration: 'none', color: 'inherit' }}>
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img src="/supportbot-logo.png" alt="SupportBot" className="logo" />
              <span className="brand">SupportBot</span>
            </a>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <span className={`status-badge ${data.status === 'solved' ? 'status-solved' : data.status === 'archived' ? 'status-archived' : 'status-open'}`}>
                {data.status === 'solved' ? 'Вирішено' : data.status === 'archived' ? 'Архів' : 'Відкрито'}
              </span>
            </div>
          </header>

          {data.status === 'archived' && (
            <div style={{
              background: '#fef3c7',
              borderBottom: '1px solid #fde68a',
              padding: '10px 20px',
              fontSize: 13,
              color: '#92400e',
              display: 'flex',
              alignItems: 'center',
              gap: 8,
            }}>
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ width: 14, height: 14, flexShrink: 0 }}>
                <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>
                <line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>
              </svg>
              Це стара версія відповіді. Актуальне рішення може відрізнятись — запитайте бота знову.
            </div>
          )}

          <main>
            <h1>{data.problem_title}</h1>
            <p className="meta">
              {data.created_at ? format(new Date(data.created_at), 'd MMM yyyy, HH:mm') : ''}
            </p>

            {data.tags && data.tags.length > 0 && (
              <div className="tags">
                {data.tags.map(tag => (
                  <span key={tag} className="tag">#{tag}</span>
                ))}
              </div>
            )}

            <div className="problem-section">
              <h2 className="section-title">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <circle cx="12" cy="12" r="10"/>
                  <line x1="12" y1="8" x2="12" y2="12"/>
                  <line x1="12" y1="16" x2="12.01" y2="16"/>
                </svg>
                Проблема
              </h2>
              <p className="section-content">{data.problem_summary}</p>
            </div>

            <div className="solution-section">
              <h2 className="section-title">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <polyline points="20 6 9 17 4 12"/>
                </svg>
                Рішення
              </h2>
              <p className="section-content">{data.solution_summary}</p>
            </div>
          </main>

          {(() => {
            const videoBlocks: { description?: string; transcript?: string }[] = [];
            data.evidence?.forEach((msg) => {
              const text = msg.content_text || '';
              const descMatch = text.match(/\[Відео:\s*[^\]]*?—\s*(.+?)\]/);
              const transMatch = text.match(/\[Транскрипт відео:\s*([\s\S]+?)\]/);
              if (descMatch || transMatch) {
                videoBlocks.push({
                  description: descMatch ? descMatch[1].trim() : undefined,
                  transcript: transMatch ? transMatch[1].trim() : undefined,
                });
              }
            });
            if (videoBlocks.length === 0) return null;
            return (
              <details className="video-transcript-section" style={{ margin: '0 0 0 0', borderTop: 'none', borderRadius: '0 0 var(--radius) var(--radius)', borderTopWidth: 0 }}>
                <summary>
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <polygon points="23 7 16 12 23 17 23 7" />
                    <rect x="1" y="5" width="15" height="14" rx="2" ry="2" />
                  </svg>
                  Відеоматеріали ({videoBlocks.length})
                </summary>
                <div className="video-transcript-body">
                  {videoBlocks.map((vb, i) => (
                    <div key={i} className="vt-block">
                      {vb.description && (
                        <>
                          <div className="vt-label">Опис</div>
                          <div>{vb.description}</div>
                        </>
                      )}
                      {vb.transcript && (
                        <>
                          <div className="vt-label" style={vb.description ? { marginTop: 8 } : {}}>Транскрипт</div>
                          <div>{vb.transcript}</div>
                        </>
                      )}
                    </div>
                  ))}
                </div>
              </details>
            );
          })()}
        </div>

        <div className="card">
          <div className="chat-header">
            <h2>
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
              </svg>
              Історія переписки
            </h2>
          </div>

          {data.evidence && data.evidence.length > 0 ? (
            <div className="messages">
              {data.evidence.filter((msg) => msg.content_text?.trim() || msg.attachments?.length || msg.images?.length).map((msg) => {
                const participantNum = senderOrder.indexOf(msg.sender_hash) + 1;
                const label = msg.sender_name || `Учасник ${participantNum}`;
                const initials = msg.sender_name
                  ? msg.sender_name.split(' ').map((w: string) => w[0]).join('').substring(0, 2).toUpperCase()
                  : `У${participantNum}`;
                return (
                  <div key={msg.message_id} className="message">
                    <div className="message-header">
                      <div className="avatar">{initials}</div>
                      <div className="sender-info">
                        <span className="sender-name">{label}</span>
                        <span className="message-time">
                          {' '}&middot; {format(new Date(msg.ts), 'd MMM, HH:mm')}
                        </span>
                      </div>
                    </div>
                    {(() => {
                      let text = msg.content_text || '';
                      // Extract transcript only (no video description shown in UI)
                      const transcriptMatch = text.match(/\[Транскрипт відео:\s*([\s\S]+?)\]/);
                      const transcript = transcriptMatch ? transcriptMatch[1].trim() : null;
                      // Strip all media markers from display text
                      text = text.replace(/\n*\[Зображення[^\]]*\]|\n*\[image\]\s*\{[\s\S]*?\}|\n*\[image\]|\n*\[attachment:[^\]]*\]|\n*\[Відео:[^\]]*\]|\n*\[Транскрипт відео:[^\]]*\]/g, '').trim();
                      // Clean up raw JSON OCR blocks
                      text = text.replace(/\{"extracted_text"\s*:[\s\S]*?\}/g, '').trim();
                      return (
                        <>
                          {text && <p className="message-text">{text}</p>}
                          {transcript && (
                            <details className="transcript-details">
                              <summary>Транскрипт відео</summary>
                              <div className="transcript-content">{transcript}</div>
                            </details>
                          )}
                        </>
                      );
                    })()}
                    {(() => {
                      const items = msg.attachments?.length
                        ? msg.attachments
                        : (msg.images || []).map((u: string) => ({ url: u, content_type: '' }));
                      if (!items.length) return null;

                      const IMAGE_EXTS = /\.(jpe?g|png|gif|webp|bmp|heic|svg)$/i;
                      const VIDEO_EXTS = /\.(mp4|webm|mov|avi|mkv)$/i;

                      const isImage = (att: Attachment) =>
                        att.content_type?.startsWith('image/') || IMAGE_EXTS.test(att.url);
                      const isVideo = (att: Attachment) =>
                        att.content_type?.startsWith('video/') || VIDEO_EXTS.test(att.url);

                      const resolveUrl = (u: string) =>
                        u.startsWith('http://') || u.startsWith('https://') ? u : `${publicApiUrl}${u}`;

                      const fileName = (u: string) => {
                        const parts = u.split('/');
                        return decodeURIComponent(parts[parts.length - 1] || 'file');
                      };

                      return (
                        <div className="message-images">
                          {items.map((att: Attachment, idx: number) => {
                            const src = resolveUrl(att.url);
                            if (isImage(att)) {
                              return (
                                <a key={idx} href={src} target="_blank" rel="noopener noreferrer">
                                  {/* eslint-disable-next-line @next/next/no-img-element */}
                                  <img src={src} alt="Attachment" />
                                </a>
                              );
                            }
                            if (isVideo(att)) {
                              return (
                                <video key={idx} controls preload="metadata">
                                  <source src={src} type={att.content_type || 'video/mp4'} />
                                </video>
                              );
                            }
                            return (
                              <a key={idx} href={src} target="_blank" rel="noopener noreferrer" className="file-download">
                                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                                  <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
                                  <polyline points="7 10 12 15 17 10"/>
                                  <line x1="12" y1="15" x2="12" y2="3"/>
                                </svg>
                                {fileName(att.url)}
                              </a>
                            );
                          })}
                        </div>
                      );
                    })()}
                  </div>
                );
              })}
              {data.closed_emoji && data.status === 'solved' && (
                <div className="emoji-confirmation">
                  <span className="emoji-bubble">{data.closed_emoji}</span>
                  Учасник підтвердив вирішення реакцією
                </div>
              )}
            </div>
          ) : (
            <div className="empty-chat">
              Історія переписки недоступна для цього кейсу
            </div>
          )}

          <footer>Academia Tech © 2026</footer>
        </div>
      </div>
    </>
  );
}
