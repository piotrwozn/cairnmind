import { useEffect } from 'react';
import type { ActiveView } from '@/types/contracts';

interface KeyboardNavigationOptions {
  itemIds: string[];
  selectedId: string | null;
  onSelectId: (id: string) => void;
  onEscape: () => void;
  onOpenCommandPalette: () => void;
  onSwitchView: (view: ActiveView) => void;
  onToggleRail: () => void;
  onToggleDock: () => void;
  paletteOpen: boolean;
}

function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  return (
    tag === 'INPUT' ||
    tag === 'TEXTAREA' ||
    tag === 'SELECT' ||
    target.isContentEditable
  );
}

/**
 * IDE-grade keyboard navigation: j/k list traversal, Esc dismiss, Cmd+K palette, 1-7 views, [ rail, ` dock.
 */
export function useKeyboardNavigation({
  itemIds,
  selectedId,
  onSelectId,
  onEscape,
  onOpenCommandPalette,
  onSwitchView,
  onToggleRail,
  onToggleDock,
  paletteOpen,
}: KeyboardNavigationOptions): void {
  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        onOpenCommandPalette();
        return;
      }

      if (event.key === 'Escape') {
        event.preventDefault();
        onEscape();
        return;
      }

      if (paletteOpen || isEditableTarget(event.target)) {
        return;
      }

      if (event.metaKey || event.ctrlKey || event.altKey) {
        return;
      }

      switch (event.key) {
        case 'j': {
          if (itemIds.length === 0) return;
          event.preventDefault();
          const currentIndex = selectedId ? itemIds.indexOf(selectedId) : -1;
          const nextIndex = currentIndex < 0 ? 0 : Math.min(itemIds.length - 1, currentIndex + 1);
          onSelectId(itemIds[nextIndex]);
          break;
        }
        case 'k': {
          if (itemIds.length === 0) return;
          event.preventDefault();
          const currentIndex = selectedId ? itemIds.indexOf(selectedId) : itemIds.length;
          const prevIndex = currentIndex <= 0 ? 0 : currentIndex - 1;
          onSelectId(itemIds[prevIndex]);
          break;
        }
        case '1':
          event.preventDefault();
          onSwitchView('matrix');
          break;
        case '2':
          event.preventDefault();
          onSwitchView('provenance');
          break;
        case '3':
          event.preventDefault();
          onSwitchView('graph');
          break;
        case '4':
          event.preventDefault();
          onSwitchView('audit');
          break;
        case '5':
          event.preventDefault();
          onSwitchView('ask');
          break;
        case '6':
          event.preventDefault();
          onSwitchView('drift');
          break;
        case '7':
          event.preventDefault();
          onSwitchView('drafts');
          break;
        case '[':
          event.preventDefault();
          onToggleRail();
          break;
        case '`':
          event.preventDefault();
          onToggleDock();
          break;
        default:
          break;
      }
    };

    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [
    itemIds,
    onEscape,
    onOpenCommandPalette,
    onSelectId,
    onSwitchView,
    onToggleDock,
    onToggleRail,
    paletteOpen,
    selectedId,
  ]);
}
