"""Inspiring Japanese / anime quotes for failed / expired challenge posts."""

from __future__ import annotations

import random

# Japanese originals only — anime / manga / kotowaza (not back-translated).
_QUOTES = (
    # --- Slam Dunk ---
    '「あきらめたらそこで試合終了ですよ」 - 安西先生',
    '「最後まで希望を捨てちゃいかん」 - 安西先生',
    '「『負けたことがある』というのが、いつか大きな財産になる」 - 堂本五郎',
    '「下手糞の上級者への道のりは己が下手さを知りて一歩目」 - 安西先生',
    # --- Naruto ---
    '「自分を信じない奴なんかに努力する価値はない!!!」 - マイト・ガイ',
    '「この技で証明しましょう。努力が天才を上回ることを」 - ロック・リー',
    '「まっすぐ自分の言葉は曲げねえ…オレの…忍道だ!!」 - うずまきナルト',
    '「オレが諦めるのを───諦めろ!!!!」 - うずまきナルト',
    '「諦めないってばよ!」 - うずまきナルト',
    '「たとえ忍術や幻術は使えなくても立派な忍者になれることを証明したいです!」 - ロック・リー',
    # --- One Piece ---
    '「もし諦めたら くいが残る!!! おれは行く!!」 - モンキー・D・ルフィ',
    '「奇跡は諦めない奴の頭上にしか降りて来ない!!!!」 - イワンコフ',
    '「二度と負けねェから!」 - ロロノア・ゾロ',
    '「海賊王におれはなる!」 - モンキー・D・ルフィ',
    '「死ぬことは恩返しじゃねえぞ!!!」 - モンキー・D・ルフィ',
    # --- Kuroko no Basuke ---
    '「諦めるな! 倒れることなど何も恥ではない…そこから起き上がらないことこそ恥!」 - 緑間真太郎',
    '「諦めなければ必ずできるとは言わねえ。けど諦めたら何も残んねぇ」 - 青峰大輝',
    '「諦めるのだけは絶対に嫌だ」 - 黒子テツヤ',
    '「人事を尽くして天命を待つ」 - 緑間真太郎',
    # --- Haikyuu!! ---
    '「諦めた時点で負け確定だ」 - 縁下力',
    '「才能は開花させるもの センスは磨くもの」 - 及川徹',
    '「ところで平凡な俺よ 下を向いている暇はあるのか」 - 日向翔陽',
    # --- Demon Slayer ---
    '「心を燃やせ」 - 煉獄杏寿郎',
    '「己の弱さや不甲斐なさにどれだけ打ちのめされようと 心を燃やせ 歯を食いしばって前を向け」 - 煉獄杏寿郎',
    '「胸を張って生きろ」 - 煉獄杏寿郎',
    # --- Bleach ---
    '「てめえが俺より強かったら…俺が諦めると思ってんのか?」 - 黒崎一護',
    '「『助けてえ』んじゃねえよ…『助ける』んだ!!!」 - 黒崎一護',
    '「恐怖を捨てろ 前を見ろ 進め 決して立ち止まるな 退けば老いるぞ 臆せば死ぬぞ!」 - 斬月',
    '「敗北が恐ろしければ強くなればいい」 - 朽木ルキア',
    # --- Hunter x Hunter ---
    '「もしオレがここであきらめたら一生会えない気がする だから退かない」 - ゴン＝フリークス',
    '「脚を切られちゃうのはいやだ! でも降参するのもいやだ!! だからもっと別のやり方で戦おう!」 - ゴン＝フリークス',
    # --- Dragon Ball ---
    '「落ちこぼれだって必死で努力すりゃエリートを超えることがあるかもよ」 - 孫悟空',
    '「無理と分かっていても、やんなきゃなんねぇ時だってあるんだ!!!!」 - 孫悟空',
    # --- My Hero Academia ---
    '「君はヒーローになれる」 - オールマイト',
    '「もう大丈夫! 何故って!? 私が来た!!」 - オールマイト',
    '「ヒーローは…守るものが多いんだよ…だから負けないんだよ」 - オールマイト',
    '「プロはいつだって命け!!!!!!」 - オールマイト',
    # --- Fairy Tail ---
    '「恐怖は悪ではない。それは己の弱さを知るということだ。弱さを知れば人は強くも優しくもなれる」 - ギルダーツ・クライヴ',
    '「負けたくれぇでぐじぐじしてんじゃねえ!! 止まる事を知らねえギルドだ!!」 - ナツ・ドラグニル',
    '「あきらめんな オレはここに立っているぞ!! 明日の分をひねり出すんだ!!」 - ナツ・ドラグニル',
    '「明日なんてわからなくていい! 今日を全力で生きる為に!!!!」 - ナツ・ドラグニル',
    # --- Other anime / manga ---
    '「俺は諦めないんで死ぬまでわかんねーっス」 - 日比野カフカ',
    '「諦めないのがオレの魔法だ!」 - アスタ',
    '「努力した者が全て報われるとは限らん。しかし成功した者は皆すべからく努力しておる!!」 - 鴨川源二',
    '「いちばんいけないのは自分なんかだめだと思いこむことだよ」 - ドラえもん',
    '「人にできて、君にできないなんてあるものか」 - ドラえもん',
    '「立って歩け 前へ進め あんたには立派な足がついてるじゃないか」 - エドワード・エルリック',
    '「俺の敵はだいたい俺です」 - 南波六太',
    '「諦めるのは最後までいっぱい頑張ってからにして下さい!」 - 黄前久美子',
    '「あきらめるな。まだ何か方法があるはずだ。なければ作り出すまでだ」 - 岡崎朋也',
    '「自分を憐れむな 自分を憐れめば人生は終わりなき悪夢だよ」 - 太宰治',
    '「美しく最後を飾りつける暇があるなら 最後まで美しく生きようじゃねーか」 - 坂田銀時',
    '「終わっちゃいねェ 失っちゃいねェ 俺の身体はまだ動く 俺の手はまだ届く」 - 坂田銀時',
    '「止まるんじゃねぇぞ…」 - オルガ・イツカ',
    '「わが生涯に一片の悔いなし」 - ラオウ',
    '「今日できることを今日やらないやつは、明日になったってできやしねぇ」 - 茂野吾郎',
    '「覚悟はいいか? 俺はできてる」 - ジョルノ・ジョバァーナ',
    '「俺を信じろ! 俺がお前を信じる!!」 - カミナ',
    '「お前のドリルは天を突くドリルだ!!」 - カミナ',
    # --- History / classics ---
    '「我事におゐて後悔をせす」 - 宮本武蔵',
    '「千日の稽古を鍛とし、万日の稽古を練とす」 - 宮本武蔵',
    '「今日は昨日の我に勝ち」 - 宮本武蔵',
    '「天は人の上に人を造らず人の下に人を造らず」 - 福沢諭吉',
    '「為せば成る 為さねば成らぬ 何事も」 - 上杉鷹山',
    # --- Books / literature ---
    '「雨にもまけず 風にもまけず」 - 宮沢賢治',
    '「世界がぜんたい幸福にならないうちは個人の幸福はあり得ない」 - 宮沢賢治',
    '「智に働けば角が立つ 情に棹させば流される 意地を通せば窮屈だ」 - 夏目漱石',
    '「恥の多い生涯を送って来ました」 - 太宰治',
    '「信実とは、決して空虚な妄想ではなかった」 - 太宰治',
    '「人の心を疑うのは、最も恥ずべき悪徳だ」 - 太宰治',
    '「志を立てて以て万事の源と為す」 - 吉田松陰',
    '「夫れ志の在る所、気も亦従ふ」 - 吉田松陰',
    '「独立自尊」 - 福沢諭吉',
    '「災難に逢う時節には 災難に逢うがよく候」 - 良寛',
    '「世の中に満つる楽しみも 苦しきも 思ひおもひの 心からこそ」 - 良寛',
    '「人生に失敗がないと 人生を失敗する」 - 斎藤茂太',
    '「無用の用」 - 荘子',
    # --- History / statesmen / classics ---
    '「勝って兜の緒を締めよ」 - 徳川家康',
    '「人の一生は重荷を負うて遠き道を行くがごとし」 - 徳川家康',
    '「鳴かぬなら鳴くまで待とう時鳥」 - 徳川家康',
    '「鳴かぬなら鳴かしてみせよう時鳥」 - 豊臣秀吉',
    '「鳴かぬなら殺してしまえ時鳥」 - 織田信長',
    '「一期一会」 - 千利休',
    '「和を以て貴しとなす」 - 聖徳太子',
    '「少年よ大志を抱け」 - ウィリアム・S・クラーク',
    '「ならぬことはならぬものです」 - 会津藩 什の掟',
    '「武士道といふは死ぬ事と見つけたり」 - 山本常朝',
    '「観の目つよく 見の目よわく」 - 宮本武蔵',
    '「兵法の道において 心の持ちやうは常の心に替る事なかれ」 - 宮本武蔵',
    '「勝ちに不思議の勝ちあり 負けに不思議の負けなし」 - 野村克也',
    '「艱難汝を玉にす」 - ことわざ',
    # --- Kotowaza / yojijukugo ---
    '「七転び八起き」 - ことわざ',
    '「失敗は成功のもと」 - ことわざ',
    '「猿も木から落ちる」 - ことわざ',
    '「石の上にも三年」 - ことわざ',
    '「継続は力なり」 - ことわざ',
    '「千里の道も一歩から」 - ことわざ',
    '「塵も積もれば山となる」 - ことわざ',
    '「雨垂れ石を穿つ」 - ことわざ',
    '「弘法も筆の誤り」 - ことわざ',
    '「努力に勝る天才なし」 - ことわざ',
    '「捲土重来」 - 四字熟語',
    '「百折不撓」 - 四字熟語',
    '「臥薪嘗胆」 - 四字熟語',
    '「不撓不屈」 - 四字熟語',
    '「雨降って地固まる」 - ことわざ',
    '「苦あれば楽あり」 - ことわざ',
    '「習うより慣れろ」 - ことわざ',
    '「大器晩成」 - ことわざ',
    '「彼も人なり我も人なり」 - ことわざ',
    '「死して後已む」 - ことわざ',
    '「人事を尽くして天命を待つ」 - ことわざ',
    '「初心忘るべからず」 - ことわざ',
    '「鉄は熱いうちに打て」 - ことわざ',
    '「案ずるより産むが易し」 - ことわざ',
    '「負けるが勝ち」 - ことわざ',
    '「青は藍より出でて藍より青し」 - ことわざ',
    '「ローマは一日にして成らず」 - ことわざ',
    '「泣いて暮らすも一生 笑って暮らすも一生」 - ことわざ',
)


def pick_shame(kind: str, fail_count: int) -> str:
    """One sourced quote. fail_count = lifetime expired challenges (incl. this one if expired)."""
    n = max(0, int(fail_count or 0))
    quote = random.choice(_QUOTES)
    if n >= 2:
        return f"{n}回. {quote}"
    return quote


def format_shame_caption(kind: str, fail_count: int) -> str:
    """Extra caption block under the card mention line."""
    n = max(0, int(fail_count or 0))
    quote = pick_shame(kind, n)
    if kind in ("expired", "abandon") and n >= 2:
        return f"📉 **{n} failures on record**\n_{quote}_"
    if kind == "fail" and n >= 2:
        return f"💀 prior losses: **{n}**\n_{quote}_"
    return f"_{quote}_"


_EMPTY_MONTH = (
    "A whole month. Zero clears. The leaderboard is a blank stare.",
    "No completions. Tadoku closed the tab out of secondhand embarrassment.",
    "The challenges waited. Nobody came. Awkward.",
    "Empty board. Even the bots stopped pretending to be impressed.",
)

_NEW_YEAR_OK = (
    "{year} challenges are loaded. Fresh year, fresh excuses - go make some clears.",
    "Happy {year}. Catalog's ready. Your streak (or lack of one) resets in spirit only.",
    "{year} is live. The easy tier is already judging your January plans.",
)

_NEW_YEAR_MODS = (
    "It's {year} and the challenge catalog is empty. Mods: the YAML isn't going to write itself.",
    "New year, same missing challenges. Someone with admin perms owes the server a catalog.",
    "{year} called. It wants challenges. The mods appear to be AFK in the wrong channel.",
    "Zero challenges for {year}. Immersion trackers everywhere; definitions nowhere. Fix it.",
)


def empty_month_shame(month_label: str) -> str:
    line = random.choice(_EMPTY_MONTH)
    return (
        f"📉 **Monthly shame · {month_label}**\n"
        f"No challenge clears on the board.\n_{line}_"
    )


def new_year_welcome(year: int, n_challenges: int) -> str:
    line = random.choice(_NEW_YEAR_OK).format(year=year)
    return (
        f"🎆 **Happy {year}** · {n_challenges} challenges loaded\n"
        f"_{line}_\n"
        f"Nothing cleared yet - that's normal. Don't let it stay that way."
    )


def new_year_mod_shame(year: int) -> str:
    line = random.choice(_NEW_YEAR_MODS).format(year=year)
    return f"📢 **{year} catalog missing**\n_{line}_"


def pick_shame_image():
    """Disappointment pool: CONGRATS_DIR/shame/, else None."""
    from app.congrats import _iter_dir_images, congrats_root  # noqa: PLC0415

    pool = _iter_dir_images(congrats_root() / "shame")
    return random.choice(pool) if pool else None


if __name__ == "__main__":
    # Small demo of what Discord captions look like
    random.seed(0)
    mention = "@Monty"
    samples = [
        ("fail", 0, f"💀 {mention} failed a check"),
        ("fail", 2, f"💀 {mention} failed a check"),
        ("expired", 1, f"⏰ {mention} ran out of time"),
        ("expired", 3, f"⏰ {mention} ran out of time"),
        ("expired", 5, f"⏰ {mention} ran out of time"),
    ]
    for kind, n, head in samples:
        print("---")
        print(head)
        print(format_shame_caption(kind, n))
