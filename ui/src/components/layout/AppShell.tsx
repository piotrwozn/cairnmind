import type { ReactNode } from 'react';
import { MotionConfig } from 'framer-motion';
import { Panel, PanelGroup, PanelResizeHandle } from 'react-resizable-panels';
import { AmbientCanvas } from '@/components/glass/AmbientCanvas';
import { GlassPanel } from '@/components/glass/GlassPanel';

interface AppShellProps {
  topBar: ReactNode;
  navRail: ReactNode;
  mainStage: ReactNode;
  inspectorDrawer: ReactNode;
  showInspector: boolean;
  bottomDock: ReactNode;
  overlay?: ReactNode;
}

export function AppShell({
  topBar,
  navRail,
  mainStage,
  inspectorDrawer,
  showInspector,
  bottomDock,
  overlay,
}: AppShellProps) {
  return (
    <MotionConfig reducedMotion="user">
      <AmbientCanvas>
        {topBar}

        {/* Middle Resizable Workspace */}
        <div className="flex min-h-0 flex-1 gap-2 overflow-hidden">
          {navRail}

          <PanelGroup direction="horizontal" className="min-w-0 flex-1">
            <Panel defaultSize={showInspector ? 66 : 100} minSize={40}>
              <GlassPanel className="flex h-full w-full flex-col overflow-hidden">
                {mainStage}
              </GlassPanel>
            </Panel>

            {showInspector && (
              <>
                <PanelResizeHandle className="mx-1 w-1 rounded-full bg-white/[0.06] transition-colors hover:bg-sky-400/50 active:bg-sky-400/70" />
                <Panel defaultSize={34} minSize={24} maxSize={52}>
                  {inspectorDrawer}
                </Panel>
              </>
            )}
          </PanelGroup>
        </div>

        {bottomDock}
        {overlay}
      </AmbientCanvas>
    </MotionConfig>
  );
}
