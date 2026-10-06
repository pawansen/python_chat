// Loaded with `node --import` when the ORIGINAL Node service is started by the
// regression harness. It only redirects aws-sdk v2 (DynamoDB + S3) to the local
// emulator via AWS.config — the Node project's files are never modified.
import { createRequire } from "module";
import path from "path";

const nodeDir = process.env.NODE_CHAT_DIR || "E:/xampp/htdocs/maidmatch-chat";
const require = createRequire(path.join(nodeDir, "package.json"));
const AWS = require("aws-sdk");
const endpoint = process.env.REGRESS_AWS_ENDPOINT;
if (endpoint) {
    AWS.config.dynamodb = { endpoint };
    AWS.config.s3 = { endpoint, s3ForcePathStyle: true };
}
