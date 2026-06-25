import test from "node:test";
import assert from "node:assert/strict";

import {
  HaodankuOrderInputError,
  buildOrderRequest,
  localLifePermissionReport,
  queryHaodankuOrders,
} from "../../scripts/haodanku_order_query.mjs";

const BASE_INPUT = Object.freeze({
  apiKey: "secret-key",
  startDate: 1719763200,
  endDate: 1719849600,
});

test("buildOrderRequest builds a redacted JD GET request", () => {
  const request = buildOrderRequest({
    ...BASE_INPUT,
    platform: "jd",
    minId: 1,
    back: 20,
    state: 1,
  });

  const url = new URL(request.url);
  assert.equal(request.method, "GET");
  assert.equal(url.hostname, "v3.api.haodanku.com");
  assert.equal(url.pathname, "/unify_jd_order_list");
  assert.equal(url.searchParams.get("apikey"), "secret-key");
  assert.equal(url.searchParams.get("min_id"), "1");
  assert.equal(url.searchParams.get("back"), "20");
  assert.equal(url.searchParams.get("state"), "1");
  assert.ok(!request.redactedUrl.includes("secret-key"));
  assert.equal(request.redactedParams.apikey, "***");
});

test("buildOrderRequest builds a douyin POST form request", () => {
  const request = buildOrderRequest({
    ...BASE_INPUT,
    platform: "douyin",
    mediaType: 1,
  });

  assert.equal(request.method, "POST");
  assert.equal(request.endpoint, "/dy_order_list");
  assert.equal(request.bodyEncoding, "multipart/form-data");
  assert.equal(request.body.get("apikey"), "secret-key");
  assert.equal(request.body.get("media_type"), "1");
});

test("buildOrderRequest requires douyin media_type", () => {
  assert.throws(
    () =>
      buildOrderRequest({
        ...BASE_INPUT,
        platform: "douyin",
      }),
    HaodankuOrderInputError
  );
});

test("buildOrderRequest validates local life privilege order window and platform id", () => {
  assert.throws(
    () =>
      buildOrderRequest({
        apiKey: "secret-key",
        platform: "local_life_privilege",
        startDate: 1719763200,
        endDate: 1719763200 + 31 * 24 * 60 * 60,
        localLifePlatform: 5,
      }),
    /30 days/
  );

  assert.throws(
    () =>
      buildOrderRequest({
        ...BASE_INPUT,
        platform: "local_life_privilege",
        localLifePlatform: 4,
      }),
    /1,2,3,5,6,7,8,9/
  );
});

test("queryHaodankuOrders returns normalized orders with fake fetch", async () => {
  const fakeFetch = async (url, init) => {
    assert.equal(init.method, "GET");
    assert.ok(url.includes("/mt_order_list"));
    return {
      ok: true,
      status: 200,
      text: async () =>
        JSON.stringify({
          code: 1,
          data: {
            min_id: 2,
            list: [
              {
                trade_id: "order-1",
                pay_price: "19.90",
              },
            ],
          },
        }),
    };
  };

  const result = await queryHaodankuOrders(
    {
      ...BASE_INPUT,
      platform: "meituan",
    },
    { fetchImpl: fakeFetch }
  );

  assert.equal(result.success, true);
  assert.equal(result.provider, "haodanku");
  assert.equal(result.platform, "meituan");
  assert.equal(result.endpoint, "/mt_order_list");
  assert.equal(result.min_id, 2);
  assert.equal(result.total, 1);
  assert.deepEqual(result.orders, [{ trade_id: "order-1", pay_price: "19.90" }]);
  assert.equal(result.request.params.apikey, "***");
});

test("queryHaodankuOrders maps Haodanku error envelopes", async () => {
  const fakeFetch = async () => ({
    ok: true,
    status: 200,
    text: async () => JSON.stringify({ code: 0, msg: "permission denied" }),
  });

  const result = await queryHaodankuOrders(
    {
      ...BASE_INPUT,
      platform: "pdd",
    },
    { fetchImpl: fakeFetch }
  );

  assert.equal(result.success, false);
  assert.equal(result.errors[0].code, "provider_bad_response");
  assert.equal(result.errors[0].message, "permission denied");
});

test("localLifePermissionReport documents the special permission checks", () => {
  const report = localLifePermissionReport();

  assert.match(report.summary, /platform/);
  assert.ok(report.special_permission_items.some((item) => item.includes("/hv_order_list")));
  assert.ok(report.special_permission_items.some((item) => item.includes("order query")));
});
