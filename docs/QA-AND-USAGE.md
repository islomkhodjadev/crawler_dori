# Predictor | How to use and test

Predictor compares 03 Dorixona branch prices with matching products from ArzonApteka and Oson. A branch run uses that branch’s synced stock and prices. The local market estimate and AI recommendation use accepted offers with verified coordinates inside the configured radius. Operators review matches and can export the results to Excel.

## Set up a test agent

1. Sign in to [Predictor](https://predictor.03dorixona.uz) with your admin account.
2. Open **Подключение → Скачать расширение (.zip)**. Extract the ZIP to a permanent folder. In Chrome, open `chrome://extensions`, enable **Developer mode**, then choose **Load unpacked** and select the folder containing `manifest.json`. To update an existing unpacked installation, replace the files in its existing folder and click **Reload**.
3. Open **Dori Price Agent**. Enter the agent password and click **Загрузить филиалы**. Select a branch. Edit the prefilled agent name if needed, for example `FOM №1 — 1`, and click **Подключить**. The server URL is preconfigured.
4. Go to **Агенты**. Check that the agent is online under the right branch. Keep Chrome open and the computer awake while collecting. For a parallel test, connect two independently installed agents and assign them to the same branch.
5. To show the device on the map, click **Передать моё местоположение** in the extension. After the popup confirms receipt, reopen **Агенты → Открыть карту**. Click the branch and device pins to see their details and Chrome’s reported device accuracy. Device sharing lasts for the current connection. The device pin and the branch’s configured pricing area serve different purposes.

## Start and review a branch comparison

1. Go to **Товары** and sync from ФОМ if the catalogue or prices are stale. Excel/CSV is also available for importing a product list. For a branch comparison, choose products stocked by the target branch in ФОМ.
2. Select two to four products and open **Сбор цен**. Choose ArzonApteka, Oson, or both. Select the branch or branches in **Точки сравнения**, set the radius (default 3 km), and click **Начать сбор**.
3. Monitor progress in **Сбор цен**. Select the named branch run in the header. With several branches selected, each branch with eligible products gets its own run. A branch with no assigned online agent waits for one.
4. Open **Проверка** to confirm product matches. Compare name, manufacturer, dosage, form, and pack size. Use **Результаты** to see branch price, competitor prices, distance and any suggested price. Expand a product row to inspect individual offers. Offers outside the radius or with unknown distance remain visible. If you have independently verified a competitor’s location, use **Указать подтверждённые координаты** on its offer.
5. Download **Скачать таблицу (Excel)** for the price summary or **Все предложения (Excel)** for individual offers.

**Очередь** starts a separate job for all imported products. Use **Сбор цен** for the selected-product tests below. Run the full-catalog queue scenarios on staging or during a coordinated retest window.

## Test scenarios

| ID | Scenario | Steps | Expected result |
| --- | --- | --- | --- |
| A1 | Agent setup and version | Install the current ZIP, load branches, select a branch, add a suffix to the agent name, connect. | **Агенты** shows the edited name, assigned branch, extension version and online state. The branch assignment survives renaming. |
| A2 | Incorrect agent password | Enter an incorrect password and click **Загрузить филиалы**, then retry with the valid password. | First attempt displays an error. The valid password loads the synced branch list. |
| A3 | Branch without coordinates | Open the branch picker and look for a branch marked **Нет координат**, if present. | The missing-location branch is unavailable in the extension picker. Its coordinates need to be supplied in ФОМ and synced. |
| M1 | Device at another address | Connect a device away from its assigned branch. Share its location, wait for server confirmation, reopen the map. | The branch stays at its ФОМ address. The device pin appears at the browser-reported point. Compare the point to the expected area with the displayed accuracy in mind. |
| M2 | Location unavailable | Open the map before sharing or test on a device where Chrome has no location permission. | The branch pin remains visible and the agent is listed below the map. A failed share gives an error instead of a confirmation. |
| M3 | Disconnect and reconnect | Share the device location, disconnect, reconnect and reopen the map. | The device pin from the previous connection is gone. Sharing again creates a pin for the current connection. |
| C1 | Selected products and parallel progress | Assign two agents to the same branch. Select four stocked products and both sources. Start from **Сбор цен**. | Both agents receive requests. The run tracks four products and reaches **4/4** on completion. Source statistics show up to eight checks in total, including errors or empty results. **Очередь** shows its own counters. |
| C2 | Branch isolation | Select two branches with distinct stock or prices and agents assigned to each. Select products, then start. | Each branch gets a separate run with that branch’s stocked products and own prices. Each branch’s workers process its run. |
| C3 | No agent for one branch | Start a branch run with its agent offline, then connect an agent assigned to that branch. | The run waits and then starts after a matching agent connects. Use **Остановить** if you need to cancel while waiting. |
| C4 | Nearby, outside and unknown offers | Run a local comparison. Expand an item with verified nearby, distant and unlocated offers. Look for differences between **Результаты** and the raw offer list. | The verified distance is measured from the configured search center. Only accepted nearby offers affect the branch estimate and recommendation. Missing distance is visible. If the sources do not provide enough located offers, the estimate can be unavailable. |
| C5 | Product mismatch | Find search results with a different dosage, form or pack size. Inspect **Проверка** and expanded results. | Conflicting results do not enter the comparison. A missing exact listing can end with no comparable price. |
| Q1 | Queue Start → Stop → Start | In a coordinated window, connect two agents. Start **Очередь**, wait for both to process tasks, stop, and start again. | The stopped run retains its counters. The new run receives tasks and counters advance without reconnecting the agents. Stop the test run when finished. |
| R1 | Rapid run switching | Switch between two completed runs with different branches or products, then switch back quickly. | Results and offers match the selected run. A late response does not replace the selected data. |
| R2 | Excel export and review | Review an eligible match, reload the run, and download both Excel files. | The review decision persists. The summary and offer files open, contain the selected run’s data, and the branch summary identifies the branch and radius. |
| P1 | Products and sync | Search using Cyrillic in both cases, move to page 2, and apply a branch filter. | Search matches regardless of Cyrillic case. Pagination advances and the branch filter shows the expected catalogue subset. |

## Record a failure

Record the scenario ID, time and timezone, Predictor run ID, extension version, agent name, assigned branch, selected source, expected result, actual result, and relevant elapsed time. Include a screenshot with passwords and unrelated personal information removed. For a map issue, include the popup’s confirmation or error, whether the point appears after reopening the map, and the reported accuracy rounded to metres.
