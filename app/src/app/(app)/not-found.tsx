import { NotFoundBody } from "@/components/not-found-body";

// A page's notFound() renders inside the (app) layout, which already draws the shell.
export default function AppNotFound() {
  return <NotFoundBody />;
}
