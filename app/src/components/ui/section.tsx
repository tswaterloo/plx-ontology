import * as React from "react";
import { cn } from "@/lib/cn";
import { Card, CardBody, CardHeader, CardTitle } from "./card";

/** A titled card with at most one header action. */
export function Section({
  title,
  action,
  children,
  className,
  bodyClassName,
  testId,
}: {
  title: React.ReactNode;
  action?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
  bodyClassName?: string;
  testId?: string;
}) {
  return (
    <Card className={className} data-testid={testId}>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
        {action}
      </CardHeader>
      <CardBody className={cn(bodyClassName)}>{children}</CardBody>
    </Card>
  );
}
