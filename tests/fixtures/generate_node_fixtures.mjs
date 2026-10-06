// Generates tests/fixtures/node_parity.json by executing the ORIGINAL Node
// modules (read-only) over input corpora. The Python unit tests assert the
// port reproduces every output. Usage:
//   node tests/fixtures/generate_node_fixtures.mjs [path-to-maidmatch-chat]
import { createRequire } from "module";
import fs from "fs";
import path from "path";
import { fileURLToPath, pathToFileURL } from "url";

const here = path.dirname(fileURLToPath(import.meta.url));
const nodeDir = process.argv[2] || process.env.NODE_CHAT_DIR || "E:/xampp/htdocs/maidmatch-chat";
const require = createRequire(path.join(nodeDir, "package.json"));
const imp = (rel) => import(pathToFileURL(path.join(nodeDir, rel)).href);

const out = {};

// ── celebrate/Joi validators ───────────────────────────────────────────────
const { errors } = require("celebrate");
const validation = await imp("src/validations/chat.validation.js");
const corpus = JSON.parse(fs.readFileSync(path.join(here, "validation_corpus.json"), "utf8"));
out.validation = [];
for (const c of corpus) {
    const mw = validation[c.validator];
    const req = { body: c.segment === "body" ? c.value : {}, query: c.segment === "query" ? c.value : {}, params: {}, headers: {}, method: "POST" };
    if (c.segment === "body" && c.value === "__undefined__") req.body = undefined;
    const result = await new Promise((resolve) => {
        mw(req, {}, (err) => {
            if (!err) return resolve({ ok: true, value: req[c.segment] === undefined ? "__undefined__" : req[c.segment] });
            const res = { status(code) { this.code = code; return this; }, send(body) { resolve({ ok: false, status: this.code, body }); }, json(body) { resolve({ ok: false, status: this.code, body }); } };
            errors()(err, req, res, () => resolve({ ok: false, passthrough: String(err) }));
        });
    });
    out.validation.push({ ...c, result });
}

// ── hashids ────────────────────────────────────────────────────────────────
const Hashids = require("hashids").default || require("hashids");
const hashids = new Hashids("G7k9B2pQ5dH8X1vR", 8, "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890");
out.hashids = [0, 1, 7, 50, 70, 7001, 123456789, "123", "70", "abc", null, undefined, -1, 1.5, "007"].map((v) => ({
    input: v === undefined ? "__undefined__" : v, output: hashids.encode(v),
}));

// ── content filter (functions are module-private: evaluate the source text) ──
const socketSrc = fs.readFileSync(path.join(nodeDir, "src/services/awsSocket.service.js"), "utf8");
const start = socketSrc.indexOf("const BLOCKED_TERMS");
const end = socketSrc.indexOf("const resolveSocketProfileImage");
const filter = new Function(socketSrc.slice(start, end) + "\nreturn { normalizeTight, hasBlockedTerm, getBlockedMessage };")();
const messages = ["hello", "Call me 9123 4567", "my Ｅｍａｉｌ is x", "great job", "see you at the mtr", "a.b(dot)c", "ｗｈａｔｓａｐｐ", "1-2-3-4-5-6-7",
    "zero\u200bwidth", "你好", "tomorrow 10am", "Line up please", "ok", "", "x(at)y", "[dot]com", "WeChat", "plain text message"];
out.filter = messages.map((m) => ({ input: m, norm: filter.normalizeTight(m), blocked: filter.hasBlockedTerm(m, filter.normalizeTight(m)) }));
out.blockedMessages = ["zh-HK", "zh-hant", "zh-CN", "zh-hans", "id", "en", "fr", "", null].map((l) => ({ locale: l, message: filter.getBlockedMessage(l) }));

// ── template placeholders (pure module) ────────────────────────────────────
const tp = await imp("src/utils/template.placeholder.util.js");
const fetchers = {
    fetchSenderProfile: async () => ({ first_name: "Amy", country_code: "+852", mobile_number: "60000000", email: "amy@oec.com" }),
    fetchWorkerProfile: async () => ({ name: "Siti", surname: "Rahayu", whatsapp_country_code: "+62", whatsapp_number: "81234567", hk_id: "A1", passport_number: "P1", country_code: "+852", mobile_number: "9", email: "s@x" }),
    fetchEmployerProfile: async () => ({ name: "Ka", surname: "Chan", chinese_name: "陳", hk_id: "Z1" }),
    fetchFriendView: async () => ({ user_id: 1, related_name: "Rel Name" }),
};
const templates = [
    ["Hi {{ worker_name }} {{worker_surname}} {{ unknown }} {{ name }} {{ mobile_number }}", "worker"],
    ["{{ full_name }} {{surname}} {{ chinese_name }} {{ employer_hk_id }} {{ worker_name }}", "employer"],
    ["{{ email }} {{ password }} {{ }} {{name}}{{name}}", "user"],
    ["no tokens", "worker"], ["$& {{ hk_id }} $1", "worker"], ["{{ worker_full_name }}", "worker"],
];
out.templates = [];
for (const [content, friendType] of templates) {
    out.templates.push({ content, friend_type: friendType,
        output: await tp.resolveTemplatePlaceholders({ template_content: content, friend_type: friendType, friend_id: 5, sender_user_id: 1 }, fetchers) });
}

// ── cache key hashing ──────────────────────────────────────────────────────
const { cacheKeyHash, cacheKeys } = await imp("src/config/cache.keys.config.js");
const hashInputs = ["abc", "", { where: { group_id: 12, user_id: 5, is_deleted: 0 }, attributes: ["id"] }, ["id", ["id", "conversation_sid"]],
    { scope: "message-list", platform: "", page: 1, limit: 10, paginationFilter: "" }, null, { a: "陳", b: [1, 2.5, true, null] }, 1.0, 12.5];
out.cacheHash = hashInputs.map((v) => ({ input: v, hash: cacheKeyHash(v) }));
out.cacheKeys = {
    message: cacheKeys.messageListKey(12, "3", "h"), userDetail: cacheKeys.userDetailKey(5, "h"), version: cacheKeys.versionConversationKey(5),
    jwt: cacheKeys.jwtBlocklistKey("jwt:blocklist:", "abc"),
};

fs.writeFileSync(path.join(here, "node_parity.json"), JSON.stringify(out, null, 1));
console.log("wrote node_parity.json", Object.fromEntries(Object.entries(out).map(([k, v]) => [k, Array.isArray(v) ? v.length : "ok"])));
