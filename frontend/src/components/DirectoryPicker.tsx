import { useEffect, useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { ArrowLeft, Folder, HardDrive, Home, Search, X } from "lucide-react";
import { api } from "../api/client";
import type { DirectoryListing } from "../api/types";
import { useI18n } from "../i18n";
import shared from "../styles/shared.module.css";
import styles from "./Sidebar.module.css";

interface DirectoryPickerProps {
  initialPath?: string;
  onSelect: (path: string) => void;
  onClose: () => void;
}

export function DirectoryPicker({ initialPath, onSelect, onClose }: DirectoryPickerProps) {
  const { t } = useI18n();
  const [listing, setListing] = useState<DirectoryListing | null>(null);
  const [filter, setFilter] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function browse(path?: string) {
    setBusy(true);
    setError(null);
    try {
      setListing(await api.browseDirectories(path));
      setFilter("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : t("无法读取目录"));
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    void browse(initialPath);
    // 打开时只读取一次初始目录；导航由当前对话框自行管理。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const directories = listing?.directories.filter((item) =>
    item.name.toLocaleLowerCase().includes(filter.toLocaleLowerCase()),
  );

  return (
    <Dialog.Root open onOpenChange={(open) => !open && onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay className={styles.dialogOverlay} />
        <Dialog.Content className={styles.pickerDialog}>
          <div className={styles.dialogHeader}>
            <div>
              <Dialog.Title>{t("选择工作区目录")}</Dialog.Title>
              <Dialog.Description>{t("浏览本机目录，选中后再添加工作区。")}</Dialog.Description>
            </div>
            <Dialog.Close asChild>
              <button type="button" className={shared.iconButton} aria-label={t("关闭目录选择器")}>
                <X size={18} />
              </button>
            </Dialog.Close>
          </div>
          <div className={styles.pickerLocation}>
            <button
              type="button"
              className={shared.iconButton}
              disabled={busy || !listing?.parent}
              onClick={() => void browse(listing?.parent ?? undefined)}
              aria-label={t("上一级目录")}
              title={t("上一级目录")}
            >
              <ArrowLeft size={17} />
            </button>
            <span title={listing?.path}>{listing?.path ?? t("正在读取目录")}</span>
            <button
              type="button"
              className={shared.iconButton}
              disabled={busy}
              onClick={() => void browse()}
              aria-label={t("用户目录")}
              title={t("用户目录")}
            >
              <Home size={17} />
            </button>
          </div>
          <div className={styles.driveList} aria-label={t("磁盘根目录")}>
            {listing?.roots.map((root) => (
              <button
                type="button"
                key={root}
                className={shared.ghostButton}
                disabled={busy}
                onClick={() => void browse(root)}
              >
                <HardDrive size={14} /> {root}
              </button>
            ))}
          </div>
          <label className={styles.pickerSearch}>
            <Search size={15} aria-hidden="true" />
            <input
              value={filter}
              onChange={(event) => setFilter(event.target.value)}
              placeholder={t("筛选当前目录")}
              aria-label={t("筛选当前目录")}
            />
          </label>
          <div className={styles.directoryList} aria-label={t("子目录")}>
            {busy && <p className={styles.dialogHint}>{t("正在读取目录")}</p>}
            {error && (
              <p className={styles.dialogError} role="alert">
                {error}
              </p>
            )}
            {!busy && !error && directories?.length === 0 && (
              <p className={styles.dialogHint}>{t("没有可显示的子目录")}</p>
            )}
            {!busy &&
              directories?.map((entry) => (
                <button
                  type="button"
                  key={entry.path}
                  className={styles.directoryItem}
                  onClick={() => void browse(entry.path)}
                >
                  <Folder size={16} aria-hidden="true" />
                  <span>{entry.name}</span>
                </button>
              ))}
          </div>
          {listing?.truncated && (
            <p className={styles.dialogHint}>
              {t("当前目录项目较多；也可以在添加表单中粘贴完整路径。")}
            </p>
          )}
          <div className={styles.dialogActions}>
            <button type="button" className={shared.ghostButton} onClick={onClose}>
              {t("取消")}
            </button>
            <button
              type="button"
              className={shared.button}
              disabled={!listing || busy}
              onClick={() => listing && onSelect(listing.path)}
            >
              {t("选择此目录")}
            </button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
