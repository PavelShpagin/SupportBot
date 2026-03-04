import Head from 'next/head';
import { useState, useEffect } from 'react';
import Link from 'next/link';

export default function Privacy() {
  const [lang, setLang] = useState<'uk' | 'en'>('uk');

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const urlLang = params.get('lang');
    if (urlLang === 'en' || urlLang === 'uk') {
      setLang(urlLang);
    } else {
      const browserLang = navigator.language.toLowerCase();
      if (browserLang.startsWith('uk') || browserLang.startsWith('ru')) {
        setLang('uk');
      } else if (browserLang.startsWith('en')) {
        setLang('en');
      }
    }
  }, []);

  const changeLang = (newLang: 'uk' | 'en') => {
    setLang(newLang);
    history.replaceState(null, '', '?lang=' + newLang);
  };

  return (
    <>
      <Head>
        <title>SupportBot — {lang === 'uk' ? 'Конфіденційність та Умови' : 'Privacy & Terms'}</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0" />
        <link rel="icon" type="image/png" href="/supportbot-logo.png" />
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
        .shell { width: 100%; max-width: 720px; }

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

        h1 { font-size: 22px; font-weight: 700; letter-spacing: -0.025em; margin-bottom: 8px; }
        .effective { color: var(--text-sec); font-size: 13px; margin-bottom: 24px; }
        h2 {
          font-size: 16px;
          font-weight: 650;
          margin: 28px 0 10px;
        }
        h2:first-of-type { margin-top: 0; }

        p, li {
          font-size: 15px;
          line-height: 1.65;
          color: var(--text);
        }

        p { margin-bottom: 12px; }

        ul { list-style: none; margin-bottom: 12px; }
        ul li {
          position: relative;
          padding: 4px 0 4px 20px;
        }
        ul li::before {
          content: "•";
          position: absolute;
          left: 6px;
          color: var(--signal-blue);
          font-weight: bold;
        }

        .back-link {
          display: inline-block;
          margin-top: 20px;
          font-size: 14px;
          color: var(--signal-blue);
          text-decoration: none;
          font-weight: 500;
        }
        .back-link:hover { text-decoration: underline; }

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
              <img src="/supportbot-logo.png" alt="SupportBot" className="logo" />
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
              <h1>Політика конфіденційності та Умови використання</h1>
              <p className="effective">Дата набуття чинності: 4 березня 2026</p>

              <h2>1. Які дані ми обробляємо</h2>
              <p>SupportBot обробляє повідомлення в Signal-групах, до яких його додано адміністратором. Це включає:</p>
              <ul>
                <li>Текст повідомлень у групі</li>
                <li>Зображення та файли, надіслані в групі (для OCR-розпізнавання та зберігання в базі знань)</li>
                <li>Метадані повідомлень: час відправки, хеш відправника (анонімізований ідентифікатор), відповіді на повідомлення</li>
              </ul>
              <p>Ми <strong>не</strong> обробляємо та не зберігаємо:</p>
              <ul>
                <li>Номери телефонів учасників (зберігаються лише односторонні хеші)</li>
                <li>Особисті повідомлення учасників між собою</li>
                <li>Повідомлення з груп, до яких бот не доданий</li>
              </ul>

              <h2>2. Як працює імпорт історії</h2>
              <p>При першому підключенні до групи адміністратор може дозволити імпорт історії повідомлень (до 45 днів). Цей процес:</p>
              <ul>
                <li>Потребує явного підтвердження від адміністратора (сканування QR-коду)</li>
                <li>Створює тимчасове підключення до Signal Desktop для отримання історії</li>
                <li>Після завершення імпорту підключення автоматично розривається з міркувань безпеки</li>
                <li>Витягує з повідомлень структуровані кейси підтримки (проблема + рішення) для бази знань</li>
              </ul>

              <h2>3. Як ми використовуємо дані</h2>
              <p>Дані використовуються виключно для:</p>
              <ul>
                <li>Формування бази знань із вирішених проблем для відповідної групи</li>
                <li>Автоматичних відповідей на нові запитання в групі на основі попереднього досвіду</li>
                <li>OCR-розпізнавання тексту на зображеннях для покращення якості бази знань</li>
              </ul>
              <p>Дані <strong>не</strong> передаються третім сторонам, не використовуються для рекламних цілей та не продаються.</p>

              <h2>4. Зберігання та безпека даних</h2>
              <ul>
                <li>Дані зберігаються на захищеному сервері</li>
                <li>Файли-вкладення зберігаються у зашифрованому хмарному сховищі (Cloudflare R2)</li>
                <li>Ідентифікатори відправників анонімізовані за допомогою односторонніх хешів</li>
                <li>Зв&apos;язок з Signal здійснюється через шифрований протокол Signal</li>
              </ul>

              <h2>5. Видалення даних</h2>
              <p>Щоб повністю видалити всі дані, пов&apos;язані з групою:</p>
              <ul>
                <li><strong>Видаліть бота з групи</strong> — це зупиняє обробку нових повідомлень</li>
                <li>Зверніться до адміністратора для видалення збережених кейсів та повідомлень</li>
              </ul>
              <p>Адміністратор групи може запросити повне видалення даних, звернувшись до нас.</p>

              <h2>6. Використання сторонніх сервісів</h2>
              <p>Для аналізу повідомлень та створення кейсів бот використовує Google Gemini API. Повідомлення передаються в API для обробки, але не зберігаються Google відповідно до їхньої політики для API-користувачів.</p>

              <h2>7. Умови використання</h2>
              <p>Додаючи SupportBot до Signal-групи або взаємодіючи з ним, ви погоджуєтесь з наступним:</p>
              <ul>
                <li>Ви маєте право (або згоду адміністратора) додавати бота до відповідної групи</li>
                <li>Учасники групи повідомлені про присутність бота та обробку повідомлень</li>
                <li>Бот надає відповіді на основі бази знань і не гарантує їх точність або повноту</li>
                <li>Ви не будете використовувати бота для обробки конфіденційної особистої інформації (медичні, фінансові дані тощо)</li>
                <li>Оператор залишає за собою право припинити обслуговування без попередження</li>
              </ul>

              <h2>8. Зміни до цієї політики</h2>
              <p>Ми можемо оновлювати цю політику. Зміни набувають чинності з моменту публікації на цій сторінці. Продовження використання бота означає прийняття оновлених умов.</p>

              <h2>9. Контакти</h2>
              <p>З питань конфіденційності або для запиту на видалення даних зверніться до адміністратора вашої групи або до нас через Signal.</p>

              <Link href="/" className="back-link">← На головну</Link>
            </main>
          ) : (
            <main>
              <h1>Privacy Policy & Terms of Service</h1>
              <p className="effective">Effective date: March 4, 2026</p>

              <h2>1. What data we process</h2>
              <p>SupportBot processes messages in Signal groups to which it has been added by an administrator. This includes:</p>
              <ul>
                <li>Text of messages in the group</li>
                <li>Images and files sent in the group (for OCR recognition and knowledge base storage)</li>
                <li>Message metadata: send time, sender hash (anonymized identifier), message replies</li>
              </ul>
              <p>We <strong>do not</strong> process or store:</p>
              <ul>
                <li>Phone numbers of group members (only one-way hashes are stored)</li>
                <li>Private messages between members</li>
                <li>Messages from groups where the bot is not a member</li>
              </ul>

              <h2>2. How history import works</h2>
              <p>When first connecting to a group, the administrator may authorize importing message history (up to 45 days). This process:</p>
              <ul>
                <li>Requires explicit authorization from the administrator (QR code scan)</li>
                <li>Creates a temporary Signal Desktop link to retrieve history</li>
                <li>Automatically disconnects after import completes for security</li>
                <li>Extracts structured support cases (problem + solution) from messages for the knowledge base</li>
              </ul>

              <h2>3. How we use the data</h2>
              <p>Data is used exclusively for:</p>
              <ul>
                <li>Building a knowledge base of solved issues for the respective group</li>
                <li>Automatically answering new questions in the group based on past experience</li>
                <li>OCR text recognition on images to improve knowledge base quality</li>
              </ul>
              <p>Data is <strong>not</strong> shared with third parties, not used for advertising, and not sold.</p>

              <h2>4. Data storage and security</h2>
              <ul>
                <li>Data is stored on a secured server</li>
                <li>File attachments are stored in encrypted cloud storage (Cloudflare R2)</li>
                <li>Sender identifiers are anonymized using one-way hashes</li>
                <li>Communication with Signal uses the encrypted Signal protocol</li>
              </ul>

              <h2>5. Data deletion</h2>
              <p>To completely remove all data associated with a group:</p>
              <ul>
                <li><strong>Remove the bot from the group</strong> — this stops processing of new messages</li>
                <li>Contact your administrator to request deletion of stored cases and messages</li>
              </ul>
              <p>The group administrator may request full data deletion by contacting us.</p>

              <h2>6. Third-party services</h2>
              <p>The bot uses Google Gemini API for message analysis and case creation. Messages are sent to the API for processing but are not retained by Google per their API user policy.</p>

              <h2>7. Terms of use</h2>
              <p>By adding SupportBot to a Signal group or interacting with it, you agree to the following:</p>
              <ul>
                <li>You have the right (or administrator consent) to add the bot to the respective group</li>
                <li>Group members are informed about the bot&apos;s presence and message processing</li>
                <li>The bot provides answers based on its knowledge base and does not guarantee their accuracy or completeness</li>
                <li>You will not use the bot to process sensitive personal information (medical, financial data, etc.)</li>
                <li>The operator reserves the right to discontinue the service without prior notice</li>
              </ul>

              <h2>8. Changes to this policy</h2>
              <p>We may update this policy. Changes take effect upon publication on this page. Continued use of the bot constitutes acceptance of the updated terms.</p>

              <h2>9. Contact</h2>
              <p>For privacy questions or data deletion requests, contact your group administrator or reach us via Signal.</p>

              <Link href="/" className="back-link">← Back to home</Link>
            </main>
          )}

          <footer>Academia Tech © 2026</footer>
        </div>
      </div>
    </>
  );
}
