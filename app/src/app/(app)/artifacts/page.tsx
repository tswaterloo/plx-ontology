import { ArtifactsStudio } from "@/components/artifacts/artifacts-studio";
import { PageIntro } from "@/components/ui/page-intro";

export default function ArtifactsPage() {
  return <ArtifactsStudio intro={<PageIntro path="/artifacts" />} />;
}
