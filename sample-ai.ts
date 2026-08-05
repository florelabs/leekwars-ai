/**
 * Very simple LeekWars AI sample in TypeScript.
 * Strategy: attack nearest enemy if possible, otherwise move toward them.
 */
function playTurn(): void {
  const enemy = getNearestEnemy();

  if (enemy == null) {
    say('No enemy in range.');
    return;
  }

  if (canUseWeapon(enemy)) {
    useWeapon(enemy);
  } else {
    moveToward(enemy);
  }
}

playTurn();
