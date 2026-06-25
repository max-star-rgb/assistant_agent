#!/usr/bin/env node

import { fileURLToPath } from "node:url";

export const DEFAULT_HAODANKU_ORDER_BASE_URL = "https://v3.api.haodanku.com";
export const MAX_ORDER_PAGE_SIZE = 100;
export const MAX_LOCAL_LIFE_RANGE_SECONDS = 30 * 24 * 60 * 60;

export const ORDER_ENDPOINTS = Object.freeze({
  jd: Object.freeze({
    path: "/unify_jd_order_list",
    method: "GET",
    required: [],
    permission: "jd platform permission and order query permission must be confirmed.",
  }),
  pdd: Object.freeze({
    path: "/unify_pdd_order_list",
    method: "GET",
    required: [],
    permission: "pdd platform permission and order query permission must be confirmed.",
  }),
  douyin: Object.freeze({
    path: "/dy_order_list",
    method: "POST",
    required: ["media_type"],
    permission: "douyin platform permission, account authorization, and order query permission must be confirmed.",
  }),
  vip: Object.freeze({
    path: "/vip_union_order_list",
    method: "GET",
    required: [],
    permission: "vip platform permission and order query permission must be confirmed.",
  }),
  kuaishou: Object.freeze({
    path: "/ks_order_list",
    method: "GET",
    required: [],
    permission: "kuaishou order query permission must be confirmed.",
  }),
  elm: Object.freeze({
    path: "/elm_order_list",
    method: "GET",
    required: [],
    permission: "taobao instant retail platform permission and order query permission must be confirmed.",
  }),
  meituan: Object.freeze({
    path: "/mt_order_list",
    method: "GET",
    required: [],
    permission: "meituan platform permission and order query permission must be confirmed.",
  }),
  tongcheng_hotel: Object.freeze({
    path: "/tc_order_list",
    method: "GET",
    required: [],
    permission: "tongcheng hotel order query permission must be confirmed.",
  }),
  local_life_privilege: Object.freeze({
    path: "/hv_order_list",
    method: "POST",
    required: ["platform"],
    permission: "local life privilege order query needs special permission.",
    maxRangeSeconds: MAX_LOCAL_LIFE_RANGE_SECONDS,
    allowedPlatformIds: Object.freeze(["1", "2", "3", "5", "6", "7", "8", "9"]),
  }),
  taobao_live: Object.freeze({
    path: "/tb_live_order_list",
    method: "GET",
    required: [],
    permission: "taobao order query permission must be confirmed.",
  }),
  idle: Object.freeze({
    path: "/idle_order_list",
    method: "GET",
    required: [],
    permission: "idle platform permission and order query permission must be confirmed.",
  }),
});

const PLATFORM_ALIASES = Object.freeze({
  jingdong: "jd",
  jd: "jd",
  pinduoduo: "pdd",
  pdd: "pdd",
  douyin: "douyin",
  dy: "douyin",
  vip: "vip",
  vipshop: "vip",
  kuaishou: "kuaishou",
  ks: "kuaishou",
  elm: "elm",
  eleme: "elm",
  taobao_instant_retail: "elm",
  meituan: "meituan",
  mt: "meituan",
  tongcheng: "tongcheng_hotel",
  tongcheng_hotel: "tongcheng_hotel",
  tc: "tongcheng_hotel",
  local_life: "local_life_privilege",
  local_life_privilege: "local_life_privilege",
  hv: "local_life_privilege",
  taobao_live: "taobao_live",
  tb_live: "taobao_live",
  idle: "idle",
  xianyu: "idle",
});

export class HaodankuOrderInputError extends Error {
  constructor(message, details = []) {
    super(message);
    this.name = "HaodankuOrderInputError";
    this.details = details;
  }
}

export function normalizeOrderPlatform(value) {
  const normalized = cleanString(value)?.toLowerCase().replaceAll("-", "_");
  if (!normalized) {
    return null;
  }
  return PLATFORM_ALIASES[normalized] ?? normalized;
}

export function buildOrderRequest(input, options = {}) {
  const normalized = normalizeOrderInput(input, options);
  const errors = validateNormalizedInput(normalized);
  if (errors.length > 0) {
    throw new HaodankuOrderInputError(errors.join("; "), errors);
  }

  const url = new URL(normalized.endpoint.path, ensureBaseUrl(normalized.baseUrl));
  const headers = { accept: "application/json" };
  let body;

  if (normalized.endpoint.method === "GET") {
    for (const [key, value] of Object.entries(normalized.params)) {
      url.searchParams.set(key, value);
    }
  } else {
    body = buildFormBody(normalized.params);
    if (body instanceof URLSearchParams) {
      headers["content-type"] = "application/x-www-form-urlencoded";
    }
  }

  return {
    provider: "haodanku",
    platform: normalized.platform,
    endpoint: normalized.endpoint.path,
    method: normalized.endpoint.method,
    url: url.toString(),
    redactedUrl: redactUrl(url),
    headers,
    body,
    bodyEncoding: body instanceof URLSearchParams
      ? "application/x-www-form-urlencoded"
      : body
        ? "multipart/form-data"
        : null,
    params: normalized.params,
    redactedParams: redactParams(normalized.params),
    permission: normalized.endpoint.permission,
  };
}

export async function queryHaodankuOrders(input, options = {}) {
  let request;
  try {
    request = buildOrderRequest(input, options);
  } catch (error) {
    if (error instanceof HaodankuOrderInputError) {
      return failedOrderResult({
        platform: normalizeOrderPlatform(input?.platform ?? input?.orderPlatform),
        code: "invalid_order_query_input",
        message: error.message,
        recoverable: true,
        details: error.details,
      });
    }
    throw error;
  }

  const fetchImpl = options.fetchImpl ?? globalThis.fetch;
  if (typeof fetchImpl !== "function") {
    return failedOrderResult({
      request,
      code: "provider_unavailable",
      message: "Node.js fetch is unavailable. Use Node.js 18+ or pass options.fetchImpl.",
      recoverable: false,
    });
  }

  try {
    const response = await fetchImpl(request.url, {
      method: request.method,
      headers: request.headers,
      body: request.body,
    });
    const payload = await readJsonResponse(response);

    if (!response.ok) {
      return failedOrderResult({
        request,
        code: httpStatusToErrorCode(response.status),
        message: `HTTP ${response.status}`,
        recoverable: response.status >= 500 || response.status === 429,
        payload,
      });
    }

    const envelopeError = haodankuEnvelopeError(payload);
    if (envelopeError) {
      return failedOrderResult({
        request,
        code: "provider_bad_response",
        message: envelopeError,
        recoverable: false,
        payload,
      });
    }

    return normalizeOrderResult(payload, request);
  } catch (error) {
    return failedOrderResult({
      request,
      code: "provider_network_error",
      message: error?.message ?? String(error),
      recoverable: true,
    });
  }
}

export function localLifePermissionReport() {
  return {
    summary:
      "Local-life promotion endpoints are mostly ordinary apikey endpoints in the interface list, but production access still requires confirming platform, account authorization, and order permissions.",
    special_permission_items: [
      "local_life_privilege order query uses /hv_order_list and needs special permission.",
      "order query and order pull permissions are stricter than activity or transfer-link permissions.",
      "meituan, taobao instant retail, and local-life privilege interfaces should be confirmed before production use.",
      "official-account authorization interfaces require account authorization when the target platform asks for it.",
    ],
    ordinary_apikey_examples: [
      "meituan_activity_list",
      "meituan_ratesurl",
      "elm_activity_list",
      "elm_activity_ratesurl",
      "tchotel_ratesurl",
      "hv_ratesurl",
    ],
    source_documents: [
      "haodanku-openapi-docs/AI使用说明.md",
      "haodanku-openapi-docs/接口目录.md",
      "haodanku-openapi-docs/平台接入规则与接口选择.md",
      "haodanku-openapi-docs/interfaces/本地生活接口.md",
      "haodanku-openapi-docs/interfaces/订单接口.md",
    ],
  };
}

export function parseCliArgs(argv) {
  const input = {};
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === "--help" || arg === "-h") {
      input.help = true;
      continue;
    }
    if (arg === "--local-life-permissions") {
      input.localLifePermissions = true;
      continue;
    }
    if (!arg.startsWith("--")) {
      throw new HaodankuOrderInputError(`Unexpected positional argument: ${arg}`);
    }
    const key = cliKeyToInputKey(arg);
    if (!key) {
      throw new HaodankuOrderInputError(`Unknown option: ${arg}`);
    }
    const value = argv[index + 1];
    if (!value || value.startsWith("--")) {
      throw new HaodankuOrderInputError(`Missing value for ${arg}`);
    }
    input[key] = value;
    index += 1;
  }
  return input;
}

function normalizeOrderInput(input = {}, options = {}) {
  const platform = normalizeOrderPlatform(input.platform ?? input.orderPlatform ?? input.endpoint);
  const endpoint = platform ? ORDER_ENDPOINTS[platform] : null;
  const params = {
    apikey: cleanString(input.apikey ?? input.apiKey ?? options.apiKey),
    min_id: cleanString(input.min_id ?? input.minId ?? 1),
    back: cleanString(input.back ?? MAX_ORDER_PAGE_SIZE),
    start_date: cleanString(input.start_date ?? input.startDate),
    end_date: cleanString(input.end_date ?? input.endDate),
  };

  assignOptional(params, "date_type", input.date_type ?? input.dateType);
  assignOptional(params, "state", input.state);
  assignOptional(params, "media_type", input.media_type ?? input.mediaType);
  assignOptional(
    params,
    "platform",
    input.local_life_platform ?? input.localLifePlatform ?? input.hvPlatform ?? input.brandPlatform
  );

  const extraParams = input.params ?? input.extraParams;
  if (extraParams && typeof extraParams === "object") {
    for (const [key, value] of Object.entries(extraParams)) {
      assignOptional(params, key, value);
    }
  }

  return {
    baseUrl: cleanString(input.baseUrl ?? options.baseUrl) ?? DEFAULT_HAODANKU_ORDER_BASE_URL,
    platform,
    endpoint,
    params: dropUndefined(params),
  };
}

function validateNormalizedInput(normalized) {
  const errors = [];
  if (!normalized.platform || !normalized.endpoint) {
    errors.push(`unsupported platform: ${normalized.platform ?? "missing"}`);
    return errors;
  }

  const params = normalized.params;
  for (const key of ["apikey", "min_id", "back", "start_date", "end_date"]) {
    if (!params[key]) {
      errors.push(`missing required parameter: ${key}`);
    }
  }
  for (const key of normalized.endpoint.required) {
    if (!params[key]) {
      errors.push(`missing required parameter for ${normalized.platform}: ${key}`);
    }
  }

  validateIntegerParam(errors, params, "min_id", { min: 1 });
  validateIntegerParam(errors, params, "back", { min: 1, max: MAX_ORDER_PAGE_SIZE });
  validateIntegerParam(errors, params, "start_date", { min: 0 });
  validateIntegerParam(errors, params, "end_date", { min: 0 });

  const startDate = Number(params.start_date);
  const endDate = Number(params.end_date);
  if (Number.isFinite(startDate) && Number.isFinite(endDate)) {
    if (endDate < startDate) {
      errors.push("end_date must be greater than or equal to start_date");
    }
    if (normalized.endpoint.maxRangeSeconds && endDate - startDate > normalized.endpoint.maxRangeSeconds) {
      errors.push("local_life_privilege start_date to end_date must not exceed 30 days");
    }
  }

  if (normalized.platform === "douyin" && params.media_type && !["1", "2"].includes(params.media_type)) {
    errors.push("douyin media_type must be 1 or 2");
  }

  if (normalized.platform === "local_life_privilege" && params.platform) {
    const allowed = normalized.endpoint.allowedPlatformIds;
    if (!allowed.includes(params.platform)) {
      errors.push("local_life_privilege platform must be one of 1,2,3,5,6,7,8,9");
    }
  }

  return errors;
}

function validateIntegerParam(errors, params, key, { min, max } = {}) {
  if (!params[key]) {
    return;
  }
  const value = Number(params[key]);
  if (!Number.isInteger(value)) {
    errors.push(`${key} must be an integer`);
    return;
  }
  if (min !== undefined && value < min) {
    errors.push(`${key} must be >= ${min}`);
  }
  if (max !== undefined && value > max) {
    errors.push(`${key} must be <= ${max}`);
  }
}

function buildFormBody(params) {
  if (typeof FormData === "function") {
    const body = new FormData();
    for (const [key, value] of Object.entries(params)) {
      body.append(key, value);
    }
    return body;
  }
  return new URLSearchParams(params);
}

async function readJsonResponse(response) {
  if (typeof response.text === "function") {
    const text = await response.text();
    return text ? JSON.parse(text) : {};
  }
  if (typeof response.json === "function") {
    return response.json();
  }
  return {};
}

function normalizeOrderResult(payload, request) {
  const data = extractData(payload);
  const orders = extractOrders(data);
  return {
    success: true,
    provider: "haodanku",
    platform: request.platform,
    endpoint: request.endpoint,
    method: request.method,
    total: orders.length,
    min_id: extractMinId(payload),
    orders,
    data,
    request: {
      method: request.method,
      url: request.redactedUrl,
      params: request.redactedParams,
    },
    permission: request.permission,
  };
}

function failedOrderResult({ request, platform, code, message, recoverable, details = [], payload }) {
  return {
    success: false,
    provider: "haodanku",
    platform: request?.platform ?? platform ?? null,
    endpoint: request?.endpoint ?? null,
    method: request?.method ?? null,
    errors: [
      {
        code,
        message,
        recoverable,
        details,
      },
    ],
    payload,
    request: request
      ? {
          method: request.method,
          url: request.redactedUrl,
          params: request.redactedParams,
        }
      : undefined,
  };
}

function extractData(payload) {
  if (payload && typeof payload === "object" && "data" in payload) {
    return payload.data;
  }
  return payload;
}

function extractOrders(data) {
  if (Array.isArray(data)) {
    return data;
  }
  if (data && typeof data === "object") {
    if (Array.isArray(data.list)) {
      return data.list;
    }
    if (Array.isArray(data.data)) {
      return data.data;
    }
    if (Array.isArray(data.items)) {
      return data.items;
    }
  }
  return [];
}

function extractMinId(payload) {
  if (!payload || typeof payload !== "object") {
    return null;
  }
  if ("min_id" in payload) {
    return payload.min_id;
  }
  if (payload.data && typeof payload.data === "object" && "min_id" in payload.data) {
    return payload.data.min_id;
  }
  return null;
}

function haodankuEnvelopeError(payload) {
  if (!payload || typeof payload !== "object" || !("code" in payload)) {
    return null;
  }
  const code = String(payload.code);
  if (code === "1" || code === "200") {
    return null;
  }
  return cleanString(payload.msg ?? payload.message) ?? `haodanku error code ${code}`;
}

function httpStatusToErrorCode(status) {
  if (status === 401) {
    return "provider_auth_failed";
  }
  if (status === 403) {
    return "provider_permission_denied";
  }
  if (status === 429) {
    return "provider_rate_limited";
  }
  if (status >= 500) {
    return "provider_bad_gateway";
  }
  if (status >= 400) {
    return "provider_bad_response";
  }
  return "provider_execution_failed";
}

function ensureBaseUrl(value) {
  const base = cleanString(value) ?? DEFAULT_HAODANKU_ORDER_BASE_URL;
  return base.endsWith("/") ? base : `${base}/`;
}

function redactUrl(url) {
  const redacted = new URL(url.toString());
  if (redacted.searchParams.has("apikey")) {
    redacted.searchParams.set("apikey", "***");
  }
  return redacted.toString();
}

function redactParams(params) {
  const redacted = { ...params };
  if (redacted.apikey) {
    redacted.apikey = "***";
  }
  return redacted;
}

function assignOptional(target, key, value) {
  const cleaned = cleanString(value);
  if (cleaned !== null) {
    target[key] = cleaned;
  }
}

function dropUndefined(value) {
  return Object.fromEntries(
    Object.entries(value).filter(([, item]) => item !== null && item !== undefined)
  );
}

function cleanString(value) {
  if (value === null || value === undefined) {
    return null;
  }
  const text = String(value).trim();
  return text || null;
}

function cliKeyToInputKey(arg) {
  return (
    {
      "--platform": "platform",
      "--api-key": "apiKey",
      "--base-url": "baseUrl",
      "--min-id": "minId",
      "--back": "back",
      "--start-date": "startDate",
      "--end-date": "endDate",
      "--date-type": "dateType",
      "--state": "state",
      "--media-type": "mediaType",
      "--local-life-platform": "localLifePlatform",
    }[arg] ?? null
  );
}

function printHelp() {
  console.log(`Usage:
  node scripts/haodanku_order_query.mjs --platform jd --start-date 1719763200 --end-date 1719849600

Options:
  --platform <name>              jd | pdd | douyin | vip | kuaishou | elm | meituan | tongcheng_hotel | local_life_privilege | taobao_live | idle
  --api-key <key>                Defaults to HAODANKU_API_KEY
  --base-url <url>               Defaults to HAODANKU_ORDER_BASE_URL, HAODANKU_BASE_URL, or ${DEFAULT_HAODANKU_ORDER_BASE_URL}
  --min-id <n>                   Defaults to 1
  --back <n>                     Defaults to 100, max 100
  --start-date <unix_seconds>    Required
  --end-date <unix_seconds>      Required
  --date-type <n>                Optional
  --state <n>                    Optional
  --media-type <1|2>             Required for douyin
  --local-life-platform <id>     Required for local_life_privilege
  --local-life-permissions       Print local-life permission report
`);
}

async function main() {
  try {
    const input = parseCliArgs(process.argv.slice(2));
    if (input.help) {
      printHelp();
      return;
    }
    if (input.localLifePermissions) {
      console.log(JSON.stringify(localLifePermissionReport(), null, 2));
      return;
    }
    const result = await queryHaodankuOrders({
      ...input,
      apiKey: input.apiKey ?? process.env.HAODANKU_API_KEY,
      baseUrl:
        input.baseUrl ??
        process.env.HAODANKU_ORDER_BASE_URL ??
        process.env.HAODANKU_BASE_URL ??
        DEFAULT_HAODANKU_ORDER_BASE_URL,
    });
    console.log(JSON.stringify(result, null, 2));
    if (!result.success) {
      process.exitCode = 1;
    }
  } catch (error) {
    console.error(JSON.stringify({ success: false, error: error?.message ?? String(error) }, null, 2));
    process.exitCode = 1;
  }
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  await main();
}
