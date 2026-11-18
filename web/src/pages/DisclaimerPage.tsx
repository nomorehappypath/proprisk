// Disclaimer: what PropRisk is, and that it comes with no warranty. Static
// text, no API call. Linked from the top bar on every screen.
export default function DisclaimerPage() {
  return (
    <main className="page">
      <h1>Disclaimer</h1>
      <div className="stack">
        <section className="card">
          <h2>An open-source demonstration</h2>
          <p>
            PropRisk is a free, open-source project. It was built to show that{' '}
            <a href="https://nomorehappypath.com" target="_blank" rel="noreferrer">
              NoMoreHappyPath
            </a>{' '}
            can build commercial-grade software. It is not a commercial product, and nobody is paid to support it.
          </p>
        </section>

        <section className="card">
          <h2>Use it at your own risk</h2>
          <p>
            PropRisk is provided "as is", with no warranty of any kind. KpiMinds LLC, NoMoreHappyPath and
            PropRisk's contributors bear no responsibility for bugs, errors, missing or out-of-date data, or
            any decision you make using it. You use PropRisk entirely at your own risk.
          </p>
        </section>

        <section className="card">
          <h2>For information only</h2>
          <p>
            PropRisk shows what public records and public data services say about a site. It is not legal,
            survey, title, engineering, environmental or appraisal advice, and public data can be out of date
            or wrong. Check the official source, which every card links or names, before you rely on it.
          </p>
        </section>

        <section className="card">
          <h2>Licence</h2>
          <p>
            PropRisk is released under the Apache License 2.0. Its sections 7 and 8 (Disclaimer of Warranty
            and Limitation of Liability) apply to every use of PropRisk. The full text is in the{' '}
            <code>LICENSE</code> file that comes with PropRisk.
          </p>
        </section>
      </div>
    </main>
  )
}
