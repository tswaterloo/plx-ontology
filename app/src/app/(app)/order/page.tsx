"use client";

import { Suspense } from "react";
import { BizRecordPage } from "@/components/biz-page";

export default function Page() {
  return (
    <Suspense>
      <BizRecordPage allowed={["purchase_order", "sales_order"]} fallbackTitle="Order" />
    </Suspense>
  );
}
