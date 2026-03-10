import Head from 'next/head';
import { useState, useEffect } from 'react';

export default function Home() {
  const [lang, setLang] = useState<'uk' | 'en'>('uk');

  useEffect(() => {
    // Check URL params first
    const params = new URLSearchParams(window.location.search);
    const urlLang = params.get('lang');
    
    if (urlLang === 'en' || urlLang === 'uk') {
      setLang(urlLang);
    } else {
      // Auto-detect browser language
      const browserLang = navigator.language.toLowerCase();
      if (browserLang.startsWith('uk') || browserLang.startsWith('ru')) {
        setLang('uk');
      } else if (browserLang.startsWith('en')) {
        setLang('en');
      }
      // Default is already 'uk' for other languages
    }
  }, []);

  const changeLang = (newLang: 'uk' | 'en') => {
    setLang(newLang);
    history.replaceState(null, '', '?lang=' + newLang);
  };

  return (
    <>
      <Head>
        <title>SupportBot</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0" />
        <link rel="icon" type="image/png" href="/favicon.png" />
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
        }

        * { margin: 0; padding: 0; box-sizing: border-box; }

        body {
          font-family: "Inter", -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
          background: var(--page-bg);
          color: var(--text);
          min-height: 100vh;
          display: flex;
          align-items: flex-start;
          justify-content: center;
          padding: 48px 20px;
          -webkit-font-smoothing: antialiased;
        }

        @media (max-width: 520px) {
          body { padding: 24px 12px; }
        }
      `}</style>

      <style jsx>{`
        .shell { width: 100%; max-width: 640px; }

        .card {
          background: var(--card-bg);
          border: 1px solid var(--border);
          border-radius: var(--radius);
          overflow: hidden;
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

        .lang-switch { display: flex; gap: 6px; }

        .lang-btn {
          padding: 6px 12px;
          font-size: 12px;
          font-weight: 600;
          font-family: inherit;
          border: 1px solid var(--border);
          border-radius: 6px;
          cursor: pointer;
          background: transparent;
          color: var(--text-sec);
          transition: all 0.12s ease;
        }

        .lang-btn:hover { border-color: var(--text-sec); }
        .lang-btn.active { background: var(--signal-blue); border-color: var(--signal-blue); color: #fff; }

        main { padding: 32px 24px 28px; }

        h1 { font-size: 22px; font-weight: 700; letter-spacing: -0.025em; margin-bottom: 16px; }
        .lead { color: var(--text-sec); font-size: 15px; line-height: 1.6; margin-bottom: 28px; }
        .lead a { color: var(--signal-blue); text-decoration: none; font-weight: 500; }
        .lead a:hover { text-decoration: underline; }

        h2 {
          font-size: 11px;
          font-weight: 600;
          text-transform: uppercase;
          letter-spacing: 0.06em;
          color: var(--text-sec);
          margin: 24px 0 12px;
        }

        h2:first-of-type { margin-top: 0; }

        ol { list-style: none; counter-reset: steps; }
        ol li {
          counter-increment: steps;
          display: flex;
          gap: 12px;
          padding: 11px 0;
          border-bottom: 1px solid var(--border);
          font-size: 15px;
          line-height: 1.55;
        }
        ol li::before {
          content: counter(steps);
          flex-shrink: 0;
          width: 24px;
          height: 24px;
          background: var(--signal-blue);
          color: #fff;
          border-radius: 50%;
          display: flex;
          align-items: center;
          justify-content: center;
          font-size: 12px;
          font-weight: 600;
          margin-top: 0px;
        }
        ol li:last-child { border-bottom: none; padding-bottom: 0; }

        ul { list-style: none; }
        ul li {
          position: relative;
          padding: 7px 0 7px 24px;
          font-size: 15px;
          line-height: 1.55;
        }
        ul li::before {
          content: "•";
          position: absolute;
          left: 8px;
          color: var(--signal-blue);
          font-weight: bold;
        }

        code {
          font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
          font-size: 13px;
          background: rgba(44, 107, 237, 0.08);
          padding: 2px 6px;
          border-radius: 4px;
        }

        .note {
          margin-top: 24px;
          padding: 12px 14px;
          border-radius: 8px;
          background: var(--page-bg);
          border: 1px solid var(--border);
          color: var(--text-sec);
          font-size: 14px;
          line-height: 1.55;
        }

        .note a {
          color: var(--signal-blue);
          text-decoration: none;
          font-weight: 500;
        }
        .note a:hover { text-decoration: underline; }

        footer {
          padding: 14px 20px;
          border-top: 1px solid var(--border);
          color: var(--text-sec);
          font-size: 12px;
          text-align: center;
        }

        @media (max-width: 520px) {
          main { padding: 24px 18px 22px; }
          h1 { font-size: 20px; }
        }
      `}</style>

      <div className="shell">
        <div className="card">
          <header>
            <div className="header-left">
              <img src="/supportbot-logo-128.png" alt="SupportBot" className="logo" />
              <span className="brand">SupportBot</span>
            </div>
            <div className="lang-switch">
              <button 
                className={`lang-btn ${lang === 'uk' ? 'active' : ''}`} 
                onClick={() => changeLang('uk')}
              >
                UA
              </button>
              <button 
                className={`lang-btn ${lang === 'en' ? 'active' : ''}`} 
                onClick={() => changeLang('en')}
              >
                EN
              </button>
            </div>
          </header>

          {lang === 'uk' ? (
            <main>
              <h1>Як працює SupportBot</h1>
              <p className="lead">Бот для Signal-груп технічної підтримки. Автоматично збирає вирішені проблеми в базу знань і відповідає на нові запитання на основі досвіду групи.</p>

              <h2>Як отримати доступ</h2>
              <p className="lead" style={{marginBottom: '16px'}}>Для підключення бота до вашої групи напишіть на <a href="mailto:support@theacademia.tech">support@theacademia.tech</a>, вказавши ваш номер телефону в Signal, назву групи та опис використання. Після підтвердження ваш номер буде додано до списку доступу.</p>

              <h2>Як додати до групи</h2>
              <ol>
                <li>Отримайте номер бота від адміністратора</li>
                <li>Додайте бота до своїх контактів у Signal (це обов&apos;язково для активації)</li>
                <li>У Signal: відкрийте групу → натисніть назву групи → «Додати учасників» → введіть номер бота</li>
                <li>Напишіть боту в особисті повідомлення назву групи</li>
                <li>Бот надішле QR-код — відскануйте його в Signal, щоб підтвердити доступ</li>
                <li>Готово — бот починає працювати</li>
              </ol>

              <h2>Як користуватися</h2>
              <ul>
                <li>Бот відповідає в групі тільки якщо є хоча б один підключений Admin у контактах</li>
                <li>Якщо Admin видаляє бота з контактів, доступ і мовні налаштування скидаються автоматично</li>
                <li>Бот відповідає лише тоді, коли впевнений у відповіді</li>
                <li>Щоб викликати бота напряму, наберіть <code>@SupportBot</code>, виберіть бота зі списку і напишіть питання</li>
                <li>Бот враховує текст, зображення та відео (включаючи транскрипцію аудіо)</li>
              </ul>

              <h2>Документація Google Docs</h2>
              <ul>
                <li>Додайте посилання на Google Docs в опис групи — бот автоматично їх проіндексує</li>
                <li>Бот рекурсивно обходить пов&apos;язані документи (документ може містити посилання на інші Google Docs)</li>
                <li>Документація переіндексується кожні 10 хвилин</li>
                <li>Бот використовує текст і зображення з документів для відповідей</li>
              </ul>

              <h2>Команди (в особистих повідомленнях боту)</h2>
              <ul>
                <li><code>/ua</code> — бот відповідатиме українською</li>
                <li><code>/en</code> — бот відповідатиме англійською</li>
                <li><code>/union Група 1, Група 2, ...</code> — об&apos;єднує базу знань та документацію кількох груп. Кейси та документи стають спільними для всіх груп в об&apos;єднанні</li>
                <li><code>/split</code> — скасовує всі об&apos;єднання, кожна група повертається до незалежної бази знань</li>
                <li><code>/wipe</code> — видаляє ВСІ дані бота (групи, кейси, історію). Реєстрація номера зберігається</li>
                <li><code>/tag Назва Групи, +380..., +380...</code> — встановлює кого тегувати (@mention) при ескалації</li>
              </ul>

              <div className="note">Бот обробляє повідомлення групи для формування бази знань. Ідентифікатори відправників анонімізовані. Видалення бота з групи зупиняє обробку. <a href="/privacy?lang=uk">Політика конфіденційності та Умови використання →</a></div>
            </main>
          ) : (
            <main>
              <h1>How SupportBot Works</h1>
              <p className="lead">A Signal bot for technical support groups. Automatically collects solved issues into a knowledge base and answers new questions using the group&apos;s past experience.</p>

              <h2>Getting access</h2>
              <p className="lead" style={{marginBottom: '16px'}}>To connect the bot to your group, email <a href="mailto:support@theacademia.tech">support@theacademia.tech</a> with your Signal phone number, group name, and use case. Once approved, your number will be added to the access list.</p>

              <h2>Adding to a group</h2>
              <ol>
                <li>Get the bot&apos;s phone number from your administrator</li>
                <li>Add the bot to your Signal contacts (required to activate group responses)</li>
                <li>In Signal: open the group → tap the group name → &quot;Add members&quot; → enter the bot&apos;s number</li>
                <li>Send the bot a direct message with the group name</li>
                <li>The bot will send a QR code — scan it in Signal to confirm access</li>
                <li>Done — the bot starts working</li>
              </ol>

              <h2>How to use</h2>
              <ul>
                <li>The bot replies in a group only when at least one linked Admin still has the bot in contacts</li>
                <li>If an Admin removes the bot from contacts, access and language preferences are reset automatically</li>
                <li>The bot replies only when it is confident in the answer</li>
                <li>To invoke it directly, type <code>@SupportBot</code>, pick the bot from the mention list, then write your question</li>
                <li>The bot considers text, images, and video (including audio transcription)</li>
              </ul>

              <h2>Google Docs documentation</h2>
              <ul>
                <li>Add Google Docs links to the group description — the bot will automatically index them</li>
                <li>The bot recursively follows linked documents (a doc can link to other Google Docs)</li>
                <li>Documentation is re-indexed every 10 minutes</li>
                <li>The bot uses text and images from documents to answer questions</li>
              </ul>

              <h2>Commands (in direct messages to the bot)</h2>
              <ul>
                <li><code>/ua</code> — bot will reply in Ukrainian</li>
                <li><code>/en</code> — bot will reply in English</li>
                <li><code>/union Group 1, Group 2, ...</code> — unifies the knowledge base and docs across multiple groups. Cases and documentation become shared across all groups in the union</li>
                <li><code>/split</code> — resets all unions, each group returns to its independent knowledge base</li>
                <li><code>/wipe</code> — deletes ALL bot data (groups, cases, history). Phone registration is kept</li>
                <li><code>/tag Group Name, +380..., +380...</code> — sets who gets @mentioned on escalation</li>
              </ul>

              <div className="note">The bot processes group messages to build a knowledge base. Sender identifiers are anonymized. Removing the bot from the group stops processing. <a href="/privacy?lang=en">Privacy Policy & Terms of Service →</a></div>
            </main>
          )}

          <footer>Academia Tech © 2026</footer>
        </div>
      </div>
    </>
  );
}
