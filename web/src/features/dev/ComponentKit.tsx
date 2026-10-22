import { Buddy } from "../../components/buddy/Buddy";
import { BuddyBubble } from "../../components/buddy/BuddyBubble";
import { CheckButton } from "../../components/quest/CheckButton";
import { Button } from "../../components/ui/Button";
import { Card } from "../../components/ui/Card";
import { Chip } from "../../components/ui/Chip";
import { ProgressBar } from "../../components/ui/ProgressBar";

export function ComponentKit() {
  if (!import.meta.env.DEV) return <p>このページは開発時だけ表示されます。</p>;
  return (
    <section className="game-screen component-kit">
      <h1>ひだまりの部品</h1>
      <div className="kit-grid">
        <Card>
          <h2>ボタン</h2>
          <div className="kit-row">
            <Button variant="sun">はじめる</Button>
            <Button>あとで</Button>
            <Button variant="quiet">静かな操作</Button>
          </div>
        </Card>
        <Card>
          <h2>チップ</h2>
          <div className="kit-row">
            <Chip tone="xp">+12 XP</Chip>
            <Chip tone="coin">340 G</Chip>
            <Chip tone="sky">18:00</Chip>
            <Chip tone="berry">大物</Chip>
            <Chip tone="grape">たね</Chip>
            <Chip>★ 15分</Chip>
          </div>
        </Card>
        <Card>
          <h2>チェックとバー</h2>
          <div className="kit-row">
            <CheckButton done={false} onClick={() => {}} />
            <CheckButton boss done={false} onClick={() => {}} />
            <CheckButton done onClick={() => {}} />
          </div>
          <ProgressBar label="部品例の進み具合" value={69} />
        </Card>
        <Card>
          <h2>相棒</h2>
          <div className="kit-row">
            <Buddy />
            <Buddy expression="happy" />
            <Buddy expression="sleepy" />
          </div>
          <BuddyBubble line="まずは1つだけ、いっしょにやろう。" />
        </Card>
      </div>
    </section>
  );
}
