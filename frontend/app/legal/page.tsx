export const metadata = {
  title: "Legal — Trade Copilot",
};

export default function LegalPage() {
  return (
    <article style={{ maxWidth: 760, lineHeight: 1.7 }}>
      <header>
        <div className="dim" style={{ fontSize: "0.85rem" }}>
          {">"} fine print
        </div>
        <h1 style={{ marginTop: "0.25rem" }}>legal &amp; risk disclosures</h1>
        <p className="dim" style={{ fontSize: "0.85rem" }}>
          Last updated: 2026
        </p>
      </header>

      <h2 className="accent">Educational use only</h2>
      <p>
        Trade Copilot is an educational and research tool. It is not
        investment advice, not a registered investment adviser, and not a
        broker-dealer. Any strategy descriptions, backtest results, win
        rates, profit factors, or signals shown in this software are for
        informational and educational purposes only. They are not
        recommendations to trade, and they are not guarantees of any future
        result.
      </p>

      <h2 className="accent">Risk of loss</h2>
      <p>
        Trading futures, foreign exchange, and contracts for difference
        carries a substantial risk of loss and is not suitable for every
        investor. You can lose more than your initial deposit. Past
        performance — including any backtests or paper-trade results — is
        not indicative of future results. You should only trade with
        capital you can afford to lose.
      </p>

      <h2 className="accent">No custody</h2>
      <p>
        Trade Copilot does not hold your funds. All trading activity occurs
        in your own brokerage account at Genesis FX or any other broker you
        connect via TradeLocker. We send signals and orders on your behalf
        when you authorize us to do so. You may pause or disconnect at any
        time.
      </p>

      <h2 className="accent">Donation model</h2>
      <p>
        Trade Copilot is free to use. Voluntary donations via Buy Me a
        Coffee are gratefully accepted but never required. Donations are
        not refundable, are not tax-deductible (we are not a registered
        non-profit), and do not entitle you to any specific level of
        service, support, profitability, or any other benefit.
      </p>

      <h2 className="accent">No warranty</h2>
      <p>
        The software is provided &quot;as is,&quot; without warranty of any
        kind, express or implied, including but not limited to the
        warranties of merchantability, fitness for a particular purpose,
        and non-infringement. The authors will not be liable for any
        claim, damages, or other liability arising from use of the
        software.
      </p>

      <h2 className="accent">Your responsibilities</h2>
      <ul>
        <li>You are responsible for the accuracy of credentials you connect.</li>
        <li>You are responsible for all trades placed in your account.</li>
        <li>You are responsible for monitoring your own positions and risk.</li>
        <li>You are responsible for compliance with the laws of your jurisdiction.</li>
      </ul>

      <h2 className="accent">Privacy</h2>
      <p>
        We collect the minimum data needed to operate: your email (used as
        a lightweight identifier), the bots you subscribe to, your chosen
        risk level, and broker session metadata. We do not sell or share
        your data. Credentials are used to open broker sessions and are
        stored encrypted at rest.
      </p>

      <p className="dim" style={{ fontSize: "0.85rem" }}>
        By using Trade Copilot you acknowledge you have read and accept
        these terms.
      </p>
    </article>
  );
}
