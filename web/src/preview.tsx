import { createContext, useContext } from "react";

/**
 * Lets anything in the tree — including tool-call cards rendered deep inside
 * CopilotKit's chat — open a project file in the right-hand preview panel.
 */
export interface PreviewApi {
  project: string;
  open: (relPath: string) => void;
  openTree: () => void;
}

const PreviewContext = createContext<PreviewApi>({
  project: "",
  open: () => {},
  openTree: () => {},
});

export const PreviewProvider = PreviewContext.Provider;

export const usePreview = () => useContext(PreviewContext);
